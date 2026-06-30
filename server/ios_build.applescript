(*
 ios_build.applescript — source for ios_build.scpt (Unity Builder Dash iOS remote).

 Compiled into .scpt on the Mac by server/patch_scpt.sh after substituting
 the {{PLACEHOLDERS}} below with values from config.json. The host (Linux or
 Windows) scp's iOS.zip into {{WORK_DIR}}/iOS.zip before invoking a command.

 Placeholders (all rewritten by patch_scpt.sh from env vars):
   {{WORK_DIR}}          Mac-side base folder (scripts + build artefacts)
   {{WIDGET_FOLDER}}     Folder name next to iOS/ with widget sources
   {{WIDGET_BUNDLE_ID}}  Widget CFBundleIdentifier
   {{WIDGET_TEAM_ID}}    Apple Developer Team ID for widget signing
   {{WIDGET_TARGET}}     Xcode target name for the widget (e.g. URLImageWidget)
   {{APP_GROUP_ID}}      App Group ID shared between app and widget
   {{SMB_USER}}          Windows SMB user (optional, Windows host only)
   {{SMB_PASS}}          Windows SMB password (optional, Windows host only)
   {{SMB_BUILD_PATH}}    Relative path on SMB share to build/iOS.zip parent

 Commands (argv[0]):
   run:<device>              stopTerminal + xcodebuild-test on <device>
   runFull:<device>          unpack + run:<device>
   unpack                    unzip iOS.zip, pod install, add widget
   stop                      kill active Terminal job
   clearCache                remove Xcode DerivedData / .pcm
   clearBuild                xcodebuild clean
   updatePod                 full pod reinstall + add widget
   addWidget                 re-run add_widget_dependency.rb
   connectMac-<winIp>-<mac>  (Windows host only) SMB-mount winIp, save winIp
                             into config.json. Linux hosts don't need this
                             since they scp iOS.zip directly.
*)

property IPADDRESS : "127.0.0.1"

-- Wrap a shell command so stdout/stderr goes to BOTH the Mac Terminal (tee)
-- AND the host over TCP:8080 (ProgressListener in Unity Builder Dash).
-- Uses a brace group `{ ... ; }` so the pipe captures ALL commands in a
-- multi-line/multi-statement string, not just the last one. Requires bash or
-- zsh for process substitution; Terminal's default on macOS is zsh.
on nccmd(cmd)
	-- ВАЖНО: без literal newline перед "; }" — в zsh-Terminal newline трактуется как Enter
	-- и команда исполняется частично, ловит `cursh>` continuation prompt.
	return "{ " & cmd & "; } 2>&1 | tee >(nc " & IPADDRESS & " 8080)"
end nccmd

-- Read host_ip from {{WORK_DIR}}/config.json (written by the host on every
-- SSH call). Returns "" if config is missing or malformed.
on readIPFromFile()
	try
		return (do shell script "python3 -c 'import json; print(json.load(open(\"{{WORK_DIR}}/config.json\")).get(\"host_ip\",\"\"))'")
	on error
		return ""
	end try
end readIPFromFile

-- Read an arbitrary key from {{WORK_DIR}}/config.json. Used for release
-- secrets (Apple ID, app-specific password, team/bundle id) so they live in
-- config.json (refreshed by the host each run) rather than baked into the
-- compiled .scpt on disk. Returns "" if missing/malformed.
on readConfigKey(keyName)
	try
		return (do shell script "python3 -c 'import json,sys; print(json.load(open(\"{{WORK_DIR}}/config.json\")).get(sys.argv[1],\"\"))' " & quoted form of keyName)
	on error
		return ""
	end try
end readConfigKey

-- App Store Connect API key (.p8) details, read from config.json. The host
-- copies the .p8 into ~/.appstoreconnect/private_keys/AuthKey_<KEYID>.p8 (the
-- standard location xcrun/altool auto-discover) on install. Using an API key
-- is the ONLY headless auth that works for `xcodebuild -exportArchive` and
-- `altool` — the Xcode GUI account is NOT visible to the command line
-- ("No Accounts" bug), but an API key bypasses accounts entirely.
on ascKeyID()
	return my readConfigKey("asc_key_id")
end ascKeyID
on ascIssuerID()
	return my readConfigKey("asc_issuer_id")
end ascIssuerID

on stopTerminal()
	-- Kill build/deploy tools running in the Terminal tab. `kill $(jobs -p)`
	-- in the shell only touches background jobs; xcodebuild / pod install
	-- run in the FOREGROUND, so `jobs -p` is empty and nothing dies. We
	-- pkill them by name instead — reliably interrupts the build. SIGINT
	-- (== Ctrl+C) lets the tool print a "User interrupted" summary and
	-- clean up, rather than leaving half-written derived data.
	-- Also kill the BUILD's caffeinate (we wrap xcodebuild/pod in
	-- `caffeinate -i -s <cmd>`): it's the PARENT, so killing xcodebuild alone
	-- leaves caffeinate holding the tab "busy" → the close below would pop a
	-- "terminate running processes?" dialog nobody can confirm on a locked Mac.
	--
	-- CRITICAL: match only `caffeinate -i -s <something>` (a trailing arg after
	-- -s), NOT the keep-awake LaunchAgent's bare `caffeinate -i -s`. Killing the
	-- agent's caffeinate would let the Mac sleep while keep-awake is still ON.
	-- launchd would respawn it (KeepAlive), but we must not fight the agent.
	try
		do shell script "pkill -INT -x xcodebuild; pkill -INT -f 'pod install'; pkill -INT -f 'pod update'; pkill -INT -f 'pod repo'; pkill -INT -f 'add_widget_dependency'; pkill -INT -f 'caffeinate -i -s .'; true"
	end try
	delay 0.3
	tell application "Terminal"
		if (count of windows) > 0 then
			-- front window / selected tab can be `missing value` even when a
			-- window exists (window mid-close, or no selected tab) → reading
			-- `busy of <missing value>` throws -1728. Guard the whole block and
			-- skip straight to forceCloseAllWindows, which closes everything
			-- safely regardless.
			try
				set activeWindow to front window
				set activeTab to selected tab of activeWindow
				if activeTab is not missing value then
					try
						tell activeTab to do script "kill $(jobs -p) 2>/dev/null; exit" in activeTab
					end try
					-- Bounded wait for the tab to idle; don't block forever if
					-- the shell is stuck.
					repeat 10 times
						if busy of activeTab is false then exit repeat
						delay 0.2
					end repeat
				end if
			end try
			my forceCloseAllWindows()
		end if
	end tell
end stopTerminal

-- Close every Terminal window WITHOUT the "terminate running processes?"
-- confirmation dialog. That dialog is fatal on a headless/sleeping/locked Mac
-- (no one to click "Terminate"), so we first SIGKILL whatever could still be
-- running in any tab, wait for the shells to report idle, then close. As a
-- last resort we close with `saving no`, which suppresses the *unsaved*
-- prompt; the kill above is what removes the *running-process* prompt.
on forceCloseAllWindows()
	-- Hard-kill anything our pipeline could have spawned so no tab stays busy.
	-- Order matters: kill the build tools, then their children (clang/swiftc/
	-- xctest spawned by xcodebuild can keep a tab "busy" after xcodebuild dies).
	--
	-- The `caffeinate -i -s .` pattern (trailing arg after -s) kills ONLY the
	-- build-wrapping caffeinate, sparing the keep-awake LaunchAgent's bare
	-- `caffeinate -i -s` — so closing a build window never lets the Mac sleep
	-- while keep-awake is enabled.
	try
		do shell script "pkill -9 -f 'caffeinate -i -s .'; pkill -9 -x xcodebuild; pkill -9 -f 'pod install'; pkill -9 -f 'pod update'; pkill -9 -f 'pod repo'; pkill -9 -f xcrun; pkill -9 -f devicectl; pkill -9 -x XCTest; pkill -9 -x clang; pkill -9 -x swiftc; pkill -9 -x ibtool; pkill -9 -x actool; true"
	end try
	delay 0.4 -- give SIGKILL time to land before we inspect "busy"
	tell application "Terminal"
		-- Wait (bounded) for tabs to drop out of "busy" after the kills land,
		-- so close() sees idle shells and shows no confirmation.
		repeat 15 times
			set anyBusy to false
			try
				repeat with w in windows
					repeat with t in tabs of w
						if busy of t is true then set anyBusy to true
					end repeat
				end repeat
			end try
			if not anyBusy then exit repeat
			delay 0.2
		end repeat
		-- If something STILL holds a tab busy (a child we didn't name), kill the
		-- tab's own shell by its tty so the window has no running process left,
		-- then close. This is the belt that removes the "terminate running
		-- processes?" modal entirely — there's nothing left to terminate.
		try
			repeat with w in windows
				repeat with t in tabs of w
					if busy of t is true then
						set ttyName to tty of t
						if ttyName is not missing value and ttyName is not "" then
							do shell script "pkill -9 -t " & (do shell script "basename " & quoted form of ttyName) & " 2>/dev/null; true"
						end if
					end if
				end repeat
			end repeat
		end try
		delay 0.2
		try
			close windows saving no
		end try
	end tell
end forceCloseAllWindows

on clearCache()
	-- Silent bulk delete via shell. Finder's `delete` moves items to Trash
	-- one-by-one and plays a sound per file — with hundreds of .pcm that's
	-- a painful stream of whooshes. rm -rf is instant and soundless.
	try
		do shell script "rm -rf \"$HOME/Library/Developer/Xcode/DerivedData/ModuleCache.noindex\"/*.pcm"
	end try
	try
		do shell script "rm -rf \"$HOME/Library/Developer/Xcode/DerivedData\"/Unity-iPhone-*"
	end try
end clearCache

on clearBuild()
	tell application "Terminal"
		do script my nccmd("cd {{WORK_DIR}}/iOS && { [ -d ./build ] && xattr -w com.apple.xcode.CreatedByBuildSystem true ./build; true; } && xcodebuild clean && rm -rf ./build/Build")
	end tell
end clearBuild

on openXcode()
	-- Open the Unity-exported workspace in Xcode so the user can inspect
	-- build settings, signing, or run a build manually. Use `open -a Xcode`
	-- rather than `tell application "Xcode" to open` — the latter requires
	-- Automation permission and silently fails the first time.
	try
		do shell script "open -a Xcode " & quoted form of "{{WORK_DIR}}/iOS/Unity-iPhone.xcworkspace"
	on error errMsg
		display dialog "Failed to open Xcode: " & errMsg buttons {"OK"} default button "OK"
	end try
end openXcode

on addWidgetToProject()
	set widgetSource to "{{WORK_DIR}}/{{WIDGET_FOLDER}}/Widgets/"
	set projectPath to "{{WORK_DIR}}/iOS/"
	set widgetDest to projectPath & "Widgets/"
	set entitlementsFile to projectPath & "Unity-iPhone.entitlements"
	set widgetPlist to projectPath & "Widgets/Info.plist"
	set appGroupID to "{{APP_GROUP_ID}}"

	-- Existence checks via shell — Finder/System Events `exists POSIX file`
	-- chokes on non-existent paths with trailing slash (error -1728).
	try
		do shell script "test -d " & quoted form of projectPath
	on error
		display dialog "Error: project folder not found at " & projectPath buttons {"OK"} default button "OK"
		return
	end try
	try
		do shell script "test -d " & quoted form of widgetSource
	on error
		display dialog "Error: widget source folder not found at " & widgetSource buttons {"OK"} default button "OK"
		return
	end try

	-- Clean, recreate, copy widget sources — all via shell (no Finder quirks)
	do shell script "rm -rf " & quoted form of widgetDest & " && mkdir -p " & quoted form of widgetDest & " && cp -R " & quoted form of widgetSource & ". " & quoted form of widgetDest

	set mainPlist to projectPath & "Info.plist"
	-- Shell-snippet for Terminal. All comments are AppleScript-side only —
	-- we don't embed `#` comments because zsh in interactive `do script`
	-- mode parses `#` as a command (no `setopt interactivecomments` in
	-- non-rc contexts), producing noisy "command not found: #" errors.
	-- Background: widget's Info.plist MUST NOT carry application-groups —
	-- that's entitlements-only. When present on the widget plist, the host
	-- Unity app crashes at launch with -[__NSCFString count]. Only the main
	-- app's entitlements file gets the group.
	--
	-- increased-memory-limit: requires the capability pre-enabled on the main
	-- App ID (developer.apple.com → Identifiers → Edit → Additional Capabilities);
	-- `-allowProvisioningUpdates` does NOT auto-register it.
	set terminalCommand to "
/usr/libexec/PlistBuddy -c 'Delete :com.apple.security.application-groups' " & entitlementsFile & " || echo 'Skip delete';
/usr/libexec/PlistBuddy -c 'Add :com.apple.security.application-groups array' " & entitlementsFile & ";
/usr/libexec/PlistBuddy -c 'Add :com.apple.security.application-groups:0 string " & appGroupID & "' " & entitlementsFile & ";
/usr/libexec/PlistBuddy -c 'Delete :com.apple.developer.kernel.increased-memory-limit' " & entitlementsFile & " || echo 'Skip delete';
/usr/libexec/PlistBuddy -c 'Add :com.apple.developer.kernel.increased-memory-limit bool true' " & entitlementsFile & ";
/usr/libexec/PlistBuddy -c 'Add :CFBundleIdentifier string {{WIDGET_BUNDLE_ID}}' " & widgetPlist & ";
MAIN_VER=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' " & mainPlist & " 2>/dev/null || echo '1');
MAIN_SHORT=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' " & mainPlist & " 2>/dev/null || echo '1.0');
/usr/bin/plutil -replace CFBundleVersion -string \"$MAIN_VER\" " & widgetPlist & ";
/usr/bin/plutil -replace CFBundleShortVersionString -string \"$MAIN_SHORT\" " & widgetPlist & ";
cd {{WORK_DIR}}/iOS;
gem list -i xcodeproj >/dev/null 2>&1 || gem install xcodeproj --no-document --user-install;
WIDGET_BUNDLE_ID='{{WIDGET_BUNDLE_ID}}' WIDGET_TEAM_ID='{{WIDGET_TEAM_ID}}' WIDGET_TARGET_NAME='{{WIDGET_TARGET}}' ruby {{WORK_DIR}}/add_widget_dependency.rb;
echo $? > /tmp/ubd_widget_exit;
touch /tmp/ubd_widget_done;
"
	-- ВАЖНО: чистим sentinel ДО запуска Terminal — иначе polling сразу видит файл от прошлого запуска.
	do shell script "rm -f /tmp/ubd_widget_done /tmp/ubd_widget_exit"
	tell application "Terminal"
		do script my nccmd(terminalCommand)
	end tell
	-- Ждём завершения add_widget_dependency.rb до 5 минут.
	do shell script "for i in $(seq 1 300); do [ -f /tmp/ubd_widget_done ] && exit 0; sleep 1; done; exit 1"
	set widgetExit to do shell script "cat /tmp/ubd_widget_exit 2>/dev/null || echo unknown"
	if widgetExit is not "0" then
		display dialog "addWidgetToProject FAILED (exit=" & widgetExit & "). Проверь лог в Terminal." buttons {"OK"} default button 1
		error "addWidget failed (exit=" & widgetExit & ")"
	end if
end addWidgetToProject

on updatePod()
	do shell script "rm -f /tmp/ubd_pod_done /tmp/ubd_pod_exit"
	tell application "Terminal"
		activate
		-- `caffeinate -i -s &` runs as a sibling that holds the no-sleep
		-- assertion for the whole chain; `kill` drops it at the end. Simpler &
		-- quote-safe vs wrapping the multi-line `&&` chain in `caffeinate sh -c`.
		do script my nccmd("cd {{WORK_DIR}}/iOS && caffeinate -i -s & CAF=$!; { \\
			pod cache clean --all && \\
			rm -rf Pods && \\
			rm -rf Podfile.lock && \\
			rm -rf Unity-iPhone.xcworkspace && \\
			pod deintegrate && \\
			pod setup && \\
			pod update && \\
			pod repo update && \\
			pod install --repo-update; }; echo $? > /tmp/ubd_pod_exit; kill $CAF 2>/dev/null; touch /tmp/ubd_pod_done")
	end tell
	-- Ждём pod install до 10 минут (с repo-update может быть долго).
	do shell script "for i in $(seq 1 600); do [ -f /tmp/ubd_pod_done ] && exit 0; sleep 1; done; exit 1"
	set podExit to do shell script "cat /tmp/ubd_pod_exit 2>/dev/null || echo unknown"
	if podExit is not "0" then
		display dialog "pod install (updatePod) FAILED (exit=" & podExit & "). Проверь лог в Terminal." buttons {"OK"} default button 1
		error "updatePod pod install failed (exit=" & podExit & ")"
	end if
	addWidgetToProject()
end updatePod

on unpack()
	-- forceCloseAllWindows (not bare `close windows`) so a still-running
	-- xcodebuild/caffeinate from a previous run can't trigger the modal
	-- "terminate running processes?" dialog — fatal on a sleeping/locked Mac.
	my forceCloseAllWindows()

	-- Delete old iOS/ directory if present (shell is reliable with missing paths)
	do shell script "rm -rf " & quoted form of "{{WORK_DIR}}/iOS"

	-- Empty trash (best-effort)
	try
		tell application "Finder"
			if (count of items in trash) > 0 then empty the trash
		end tell
	end try

	-- If local iOS.zip is missing but SMB mount has it, copy over. Silent fallback.
	try
		do shell script "test -f " & quoted form of "{{WORK_DIR}}/iOS.zip"
	on error
		try
			do shell script "cp /Volumes/Users/{{SMB_BUILD_PATH}}/iOS.zip " & quoted form of "{{WORK_DIR}}/iOS.zip"
		end try
	end try

	-- Verify zip is here before unzipping
	try
		do shell script "test -f " & quoted form of "{{WORK_DIR}}/iOS.zip"
	on error
		display dialog "Error: iOS.zip not found in {{WORK_DIR}}. Did the host SCP fail?" buttons {"OK"} default button "OK"
		return
	end try

	-- Start marker so host sees unpack actually fired, even if unzip is silent
	try
		do shell script "echo 'unpack: starting unzip' | nc -w 1 " & IPADDRESS & " 8080"
	end try

	-- unzip separately (not piped to nc) so it ALWAYS finishes, even if the
	-- host isn't listening. After it's done, push the captured output via nc.
	set unzipLog to ""
	try
		set unzipLog to (do shell script "cd " & quoted form of "{{WORK_DIR}}" & " && unzip -o iOS.zip 2>&1")
	on error errMsg
		try
			do shell script "echo " & quoted form of ("unzip failed: " & errMsg) & " | nc -w 1 " & IPADDRESS & " 8080"
		end try
		display dialog "unzip failed: " & errMsg buttons {"OK"} default button "OK"
		return
	end try

	-- Best-effort log stream back to host; don't fail the whole unpack if nc errors
	try
		do shell script "printf '%s\\n' " & quoted form of unzipLog & " | nc -w 1 " & IPADDRESS & " 8080"
	end try
	try
		do shell script "echo 'unpack: unzip done' | nc -w 1 " & IPADDRESS & " 8080"
	end try

	-- Patch project.pbxproj: Unity on Linux bakes absolute paths like
	-- "/home/pavel/DEV/KartotekaAR/build AR/iOS/testIcon.png" into file
	-- references. On Mac those paths don't exist → xcodebuild fails with
	-- "no such file". Rewrite them to $(SRCROOT)/<filename> so Xcode
	-- resolves them to the iOS/ folder on Mac.
	try
		do shell script "sed -i '' -E 's|/home/[^\"]*/[Ii][Oo][Ss]/|$(SRCROOT)/|g' " & quoted form of "{{WORK_DIR}}/iOS/Unity-iPhone.xcodeproj/project.pbxproj"
	end try

	-- Sanitize Info.plist — some Unity post-process-build scripts (notably
	-- Google Sign-In with GIDClientID) recursively inject unrelated keys
	-- into every dict they touch, polluting UIApplicationSceneManifest.
	-- When UISceneConfigurations contains stray string values instead of
	-- only role keys mapping to arrays of scene-config dicts, UIKit in
	-- UIApplicationMain calls `count` on a string →
	--   *** NSInvalidArgumentException: -[__NSCFString count]: unrecognized selector
	-- Keep only known-good roles and valid scene-config keys.
	set infoClean to "
import plistlib
p = '{{WORK_DIR}}/iOS/Info.plist'
d = plistlib.load(open(p,'rb'))
sm = d.get('UIApplicationSceneManifest')
if isinstance(sm, dict):
    out = {}
    if 'UIApplicationSupportsMultipleScenes' in sm:
        out['UIApplicationSupportsMultipleScenes'] = sm['UIApplicationSupportsMultipleScenes']
    cfgs = sm.get('UISceneConfigurations')
    if isinstance(cfgs, dict):
        roles = {
            'UIWindowSceneSessionRoleApplication',
            'UIWindowSceneSessionRoleExternalDisplay',
            'UIWindowSceneSessionRoleVolumetricApplication',
        }
        valid = {'UISceneClassName','UISceneConfigurationName',
                 'UISceneDelegateClassName','UISceneStoryboardFile'}
        clean = {}
        for role, arr in cfgs.items():
            if role not in roles or not isinstance(arr, list):
                continue
            items = []
            for it in arr:
                if isinstance(it, dict):
                    items.append({k:v for k,v in it.items() if k in valid})
            if items:
                clean[role] = items
        if not clean:
            clean = {'UIWindowSceneSessionRoleApplication': [
                {'UISceneConfigurationName':'Default Configuration',
                 'UISceneDelegateClassName':'UnityScene'}]}
        out['UISceneConfigurations'] = clean
    d['UIApplicationSceneManifest'] = out
    plistlib.dump(d, open(p,'wb'))
    print('Info.plist UIApplicationSceneManifest sanitized')
"
	try
		do shell script "python3 -c " & quoted form of infoClean
	end try

	-- Patch Podfile post_install:
	--   IPHONEOS_DEPLOYMENT_TARGET=15.0 — FirebaseAuth/Analytics требуют iOS 13+; ставим 15.0
	--     потому что Unity тоже ставит 15.0 (см. ProjectSettings.iOSTargetOSVersionString).
	--   ENABLE_USER_SCRIPT_SANDBOXING=NO — Xcode 15+ sandboxing breaks gRPC-Core
	--     libtool step with "Command Libtool failed with a nonzero exit code";
	--     gRPC's header-symlink script phase can't write inside the sandbox.
	--   ALWAYS_OUT_OF_DATE=YES on script phases — silences Xcode's
	--     "Run script build phase will be run during every build" warnings
	--     emitted for Unity's GameAssembly + gRPC/BoringSSL/abseil.
	-- Versioned marker ("# ubd-post-install v4") lets us re-patch when we
	-- add settings in a newer version without duplicating the block.
	set podfilePatch to "
# ubd-post-install v4
post_install do |installer|
  installer.pods_project.targets.each do |target|
    target.build_configurations.each do |config|
      config.build_settings['IPHONEOS_DEPLOYMENT_TARGET'] = '15.0'
      config.build_settings['ENABLE_USER_SCRIPT_SANDBOXING'] = 'NO'
      config.build_settings['ALWAYS_EMBED_SWIFT_STANDARD_LIBRARIES'] = 'NO'
      config.build_settings['ENABLE_APP_SHORTCUTS_FLEXIBLE_MATCHING'] = 'NO'
    end
    target.build_phases.each do |phase|
      if phase.respond_to?(:shell_script)
        phase.always_out_of_date = '1'
      end
    end
  end
end
"
	-- Python strips any old post_install / legacy ubd-post-install block
	-- from Podfile, then appends the current one. Idempotent across runs.
	set pyStrip to "
import re, sys
p = '{{WORK_DIR}}/iOS/Podfile'
s = open(p).read()
# Drop any previous ubd-post-install block (with its marker comment) and
# any legacy bare post_install hook we may have appended earlier.
s = re.sub(r'\\n# ubd-post-install v\\d+\\n\\s*post_install do .*?\\nend\\n', '\\n', s, flags=re.S)
s = re.sub(r'\\npost_install do \\|installer\\|\\s*\\n\\s*installer\\.pods_project\\.targets\\.each.*?\\n\\s*end\\s*\\n\\s*end\\s*\\n\\s*end\\s*\\n', '\\n', s, flags=re.S)
open(p,'w').write(s.rstrip() + '\\n')
"
	try
		do shell script "python3 -c " & quoted form of pyStrip
		do shell script "printf '%s' " & quoted form of podfilePatch & " >> " & quoted form of "{{WORK_DIR}}/iOS/Podfile"
	end try

	-- ВАЖНО: чистим sentinel-файлы ДО запуска Terminal — иначе polling сразу видит файл от прошлого падения и не ждёт.
	do shell script "rm -f /tmp/ubd_pod_done /tmp/ubd_pod_exit"

	tell application "Terminal"
		activate
		-- Sentinel: /tmp/ubd_pod_done создаётся когда pod install завершился (успешно или нет), exit code → /tmp/ubd_pod_exit
		-- caffeinate -i -s wraps pod install — it's the longest pre-build phase
		-- (up to 10 min). Without it the Mac can idle-sleep mid-install, drop
		-- sshd, and the host loses the build with "server not responding".
		do script my nccmd("cd {{WORK_DIR}}/iOS && caffeinate -i -s pod install; echo $? > /tmp/ubd_pod_exit; touch /tmp/ubd_pod_done")
	end tell
	-- Ждём pod install до 10 минут.
	do shell script "for i in $(seq 1 600); do [ -f /tmp/ubd_pod_done ] && exit 0; sleep 1; done; exit 1"

	-- Проверяем exit code pod install и наличие workspace.
	set podExit to do shell script "cat /tmp/ubd_pod_exit 2>/dev/null || echo unknown"
	if podExit is not "0" then
		display dialog "pod install FAILED (exit=" & podExit & "). Открой Terminal, посмотри ошибку, исправь и перезапусти билд." buttons {"OK"} default button 1
		error "pod install failed (exit=" & podExit & ")"
	end if
	if not (do shell script "[ -d '{{WORK_DIR}}/iOS/Unity-iPhone.xcworkspace' ] && echo ok || echo missing") is "ok" then
		display dialog "Unity-iPhone.xcworkspace не создан после pod install. Проверь Podfile и pod install лог в Terminal." buttons {"OK"} default button 1
		error "xcworkspace missing"
	end if

	my forceCloseAllWindows()

	addWidgetToProject()
end unpack

-- Build + test: runs the app through xctest, which auto-launches it on device.
-- Works for most Unity projects; some crash during test-runner init
-- (NSInvalidArgumentException in UIApplicationMain). If that happens, use
-- `installDevice` mode instead (Settings → iOS → "Run mode: Install only").
on runDevice(deviceName)
	stopTerminal()
	tell application "Terminal"
		-- caffeinate -i -s wraps xcodebuild so the Mac can't idle-sleep during
		-- a long build. Critical on Apple Silicon laptops on battery: if it
		-- dozes, sshd dies and the host loses the build mid-flight. caffeinate
		-- holds the assertion only while xcodebuild runs, then releases it.
		do script my nccmd("cd {{WORK_DIR}}/iOS && caffeinate -i -s xcodebuild -workspace Unity-iPhone.xcworkspace -scheme Unity-iPhone -destination 'platform=iOS,name=" & deviceName & "' -allowProvisioningUpdates test")
	end tell
end runDevice

-- Build + install (no test-runner). App icon lands on the device; user
-- taps to launch normally, avoiding xctest's init quirks.
--
-- Destination uses the specific device *name*, not 'generic/platform=iOS'.
-- Why: devicectl install verifies the embedded provisioning profile includes
-- the device's UDID. A generic build uses a profile without any UDIDs
-- attached → install fails with CoreDeviceError 1002 "No provider was found."
-- Pinning to the device makes `-allowProvisioningUpdates` register this UDID
-- and bake it into the profile embedded in the .app.
on installDevice(deviceName)
	stopTerminal()
	-- caffeinate -i -s prefixes only xcodebuild (the long-running step). The
	-- subsequent `&& APP_PATH=… && xcrun …` run in the same Terminal shell
	-- right after, so install completes before any idle-sleep could trigger.
	-- Keeps the Mac awake during the build on an Apple Silicon laptop on
	-- battery, where dozing would drop sshd and lose the build mid-flight.
	set cmd to "cd {{WORK_DIR}}/iOS && caffeinate -i -s xcodebuild -workspace Unity-iPhone.xcworkspace -scheme Unity-iPhone -configuration Debug -destination 'platform=iOS,name=" & deviceName & "' -allowProvisioningUpdates -derivedDataPath build/DerivedData build && APP_PATH=$(/usr/bin/find build/DerivedData/Build/Products -maxdepth 3 -type d -name '*.app' | /usr/bin/grep -v Tests | /usr/bin/head -1) && echo \"Installing $APP_PATH on " & deviceName & "\" && xcrun devicectl device install app --device '" & deviceName & "' \"$APP_PATH\""
	tell application "Terminal"
		do script my nccmd(cmd)
	end tell
end installDevice

-- ── Release pipeline: Archive → Validate → Distribute (App Store) ──
--
-- Three separate steps so the user controls each. All sign for the App Store
-- (Apple Distribution cert + App Store provisioning profile) and rely on
-- `-allowProvisioningUpdates` to create/refresh them via the Apple ID logged
-- into Xcode. Auth for validate/upload is Apple ID + app-specific password,
-- read from config.json at runtime (not baked into the .scpt).
--
-- Layout under {{WORK_DIR}}/iOS/:
--   build/Unity-iPhone.xcarchive   ← archiveApp
--   build/export/*.ipa             ← validateApp (exportArchive)

-- ExportOptions.plist for App Store Connect export. Written fresh each run so a
-- Team ID change in config.json takes effect. teamID empty → xcodebuild infers.
on writeExportOptions(teamID)
	set plistPath to "{{WORK_DIR}}/iOS/build/ExportOptions.plist"
	do shell script "mkdir -p {{WORK_DIR}}/iOS/build"
	set teamLine to ""
	if teamID is not "" then set teamLine to "<key>teamID</key><string>" & teamID & "</string>"
	-- method=app-store-connect (Xcode 15.3+ name; old "app-store" is deprecated
	-- and prints a warning on every export).
	set xml to "<?xml version=\"1.0\" encoding=\"UTF-8\"?><!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\"><plist version=\"1.0\"><dict><key>method</key><string>app-store-connect</string><key>destination</key><string>export</string><key>signingStyle</key><string>automatic</string>" & teamLine & "</dict></plist>"
	do shell script "printf '%s' " & quoted form of xml & " > " & quoted form of plistPath
	return plistPath
end writeExportOptions

-- Run one release step via release.sh (archive|validate|distribute). The heavy
-- logic lives in {{WORK_DIR}}/release.sh — keeping the giant xcodebuild/altool
-- lines OUT of the .scpt (they used to truncate and hide all progress). The
-- script emits "[n/m] …" lines that the host ProgressListener renders live via
-- the Terminal `tee >(nc …)` wrapper. We only add: sentinel files (so the host
-- can poll completion + exit code) and a success/fail notification.
on runReleaseStep(action)
	set sentinel to "/tmp/ubd_" & action
	-- stdbuf -oL/-eL → line-buffered so nc streams progress live, not at the end.
	set cmd to "cd {{WORK_DIR}} && rm -f " & sentinel & "_done " & sentinel & "_exit; WORK_DIR={{WORK_DIR}} stdbuf -oL -eL bash {{WORK_DIR}}/release.sh " & action & "; RC=$?; echo $RC > " & sentinel & "_exit; touch " & sentinel & "_done; if [ \"$RC\" = \"0\" ]; then osascript -e \"display notification \\\"" & action & " succeeded\\\" with title \\\"iOS Release\\\" sound name \\\"Glass\\\"\"; else osascript -e \"display notification \\\"" & action & " FAILED (exit $RC)\\\" with title \\\"iOS Release\\\" sound name \\\"Basso\\\"\"; fi"
	tell application "Terminal"
		activate
		do script my nccmd(cmd)
	end tell
end runReleaseStep

-- xcodebuild archive → build/Unity-iPhone.xcarchive (Release, App Store signed).
on archiveApp()
	stopTerminal()
	my runReleaseStep("archive")
end archiveApp

-- exportArchive → .ipa, then `altool --validate-app` against App Store Connect.
-- Auth is via App Store Connect API key (read from config by release.sh).
on validateApp()
	stopTerminal()
	set keyID to my ascKeyID()
	if keyID is "" or my ascIssuerID() is "" then
		display dialog "App Store Connect API key not set (iOS settings: Key ID + Issuer ID + .p8). Validate needs them." buttons {"OK"} default button 1
		error "missing API key"
	end if
	-- ExportOptions.plist still written here so a Team ID change takes effect.
	my writeExportOptions(my readConfigKey("release_team_id"))
	my runReleaseStep("validate")
end validateApp

-- altool --upload-app → uploads the exported .ipa to App Store Connect.
on distributeApp()
	stopTerminal()
	if my ascKeyID() is "" or my ascIssuerID() is "" then
		display dialog "App Store Connect API key not set (iOS settings: Key ID + Issuer ID + .p8). Distribute needs them." buttons {"OK"} default button 1
		error "missing API key"
	end if
	my runReleaseStep("distribute")
end distributeApp

-- ── Windows-host legacy: SMB mount for reading iOS.zip from a shared folder ──
-- Linux and Windows-10+ hosts should use scp instead and ignore these handlers.

on splitString(someString)
	set firstPart to ""
	set secondPart to ""
	set thirdPart to ""
	try
		set tempTID to AppleScript's text item delimiters
		set AppleScript's text item delimiters to "-"
		set pieces to text items of someString
		set AppleScript's text item delimiters to tempTID
		set firstPart to item 1 of pieces
		if (count of pieces) >= 2 then set secondPart to item 2 of pieces
		if (count of pieces) >= 3 then set thirdPart to item 3 of pieces
	end try
	return {firstPart, secondPart, thirdPart}
end splitString

on removeBuildAlias()
	do shell script "rm -f " & quoted form of "{{WORK_DIR}}/build"
end removeBuildAlias

on connectToServer(ipa)
	try
		tell application "Finder"
			mount volume "smb://{{SMB_USER}}:{{SMB_PASS}}@" & ipa & "/Users"
			try
				make new alias file at desktop to POSIX file ("/Volumes/Users/{{SMB_BUILD_PATH}}/") with properties {name:"build"}
			on error errMsg
				display dialog "Error creating alias: " & errMsg
			end try
		end tell
	on error errMsg
		display dialog "Error connecting to server: " & errMsg
	end try
end connectToServer

on run argv
	set IPADDRESS to readIPFromFile()
	if IPADDRESS is "" then set IPADDRESS to "127.0.0.1"

	if (count of argv) = 0 then return
	set command to item 1 of argv

	if command starts with "runFull:" then
		set deviceName to text 9 thru -1 of command
		unpack()
		runDevice(deviceName)
	else if command starts with "run:" then
		set deviceName to text 5 thru -1 of command
		runDevice(deviceName)
	else if command starts with "installFull:" then
		set deviceName to text 13 thru -1 of command
		unpack()
		installDevice(deviceName)
	else if command starts with "install:" then
		set deviceName to text 9 thru -1 of command
		installDevice(deviceName)
	else if command starts with "connectMac-" then
		-- Windows-host legacy: SMB-mount the Windows share.
		-- Save winIp into config.json so subsequent actions target it.
		set {_cmd, winIp, macIp} to splitString(command)
		try
			do shell script "python3 -c \"import json,os; p='{{WORK_DIR}}/config.json'; d=json.load(open(p)) if os.path.isfile(p) else {}; d['host_ip']='" & winIp & "'; open(p,'w').write(json.dumps(d,indent=2))\""
		end try
		removeBuildAlias()
		connectToServer(winIp)
	else if command is "unpack" then
		unpack()
	else if command is "stop" then
		stopTerminal()
	else if command is "clearCache" then
		clearCache()
	else if command is "updatePod" then
		updatePod()
	else if command is "addWidget" then
		addWidgetToProject()
	else if command is "clearBuild" then
		clearBuild()
	else if command is "openXcode" then
		openXcode()
	else if command is "archiveApp" then
		archiveApp()
	else if command is "validateApp" then
		validateApp()
	else if command is "distributeApp" then
		distributeApp()
	end if
end run
