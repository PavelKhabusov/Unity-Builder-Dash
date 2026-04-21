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
	return "{ " & cmd & "
 ; } 2>&1 | tee >(nc " & IPADDRESS & " 8080)"
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

on stopTerminal()
	-- Kill build/deploy tools running in the Terminal tab. `kill $(jobs -p)`
	-- in the shell only touches background jobs; xcodebuild / pod install
	-- run in the FOREGROUND, so `jobs -p` is empty and nothing dies. We
	-- pkill them by name instead — reliably interrupts the build. SIGINT
	-- (== Ctrl+C) lets the tool print a "User interrupted" summary and
	-- clean up, rather than leaving half-written derived data.
	try
		do shell script "pkill -INT -x xcodebuild; pkill -INT -f 'pod install'; pkill -INT -f 'pod update'; pkill -INT -f 'pod repo'; pkill -INT -f 'add_widget_dependency'; true"
	end try
	delay 0.3
	tell application "Terminal"
		if (count of windows) > 0 then
			set activeWindow to front window
			set activeTab to selected tab of activeWindow
			try
				tell activeTab to do script "kill $(jobs -p) 2>/dev/null; exit" in activeTab
			end try
			-- Don't block indefinitely waiting for the tab to idle — if the
			-- shell is stuck it'll never go un-busy. Bounded wait then close.
			repeat 10 times
				if busy of activeTab is false then exit repeat
				delay 0.2
			end repeat
			try
				close activeWindow saving no
			end try
		end if
	end tell
end stopTerminal

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
	set terminalCommand to "
/usr/libexec/PlistBuddy -c 'Delete :com.apple.security.application-groups' " & entitlementsFile & " || echo 'Skip delete';
/usr/libexec/PlistBuddy -c 'Add :com.apple.security.application-groups array' " & entitlementsFile & ";
/usr/libexec/PlistBuddy -c 'Add :com.apple.security.application-groups:0 string " & appGroupID & "' " & entitlementsFile & ";
/usr/libexec/PlistBuddy -c 'Add :CFBundleIdentifier string {{WIDGET_BUNDLE_ID}}' " & widgetPlist & ";
MAIN_VER=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' " & mainPlist & " 2>/dev/null || echo '1');
MAIN_SHORT=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' " & mainPlist & " 2>/dev/null || echo '1.0');
/usr/bin/plutil -replace CFBundleVersion -string \"$MAIN_VER\" " & widgetPlist & ";
/usr/bin/plutil -replace CFBundleShortVersionString -string \"$MAIN_SHORT\" " & widgetPlist & ";
cd {{WORK_DIR}}/iOS;
gem list -i xcodeproj >/dev/null 2>&1 || gem install xcodeproj --no-document --user-install;
WIDGET_BUNDLE_ID='{{WIDGET_BUNDLE_ID}}' WIDGET_TEAM_ID='{{WIDGET_TEAM_ID}}' WIDGET_TARGET_NAME='{{WIDGET_TARGET}}' ruby {{WORK_DIR}}/add_widget_dependency.rb;
"
	tell application "Terminal"
		do script my nccmd(terminalCommand)
	end tell
end addWidgetToProject

on updatePod()
	tell application "Terminal"
		activate
		do script my nccmd("cd {{WORK_DIR}}/iOS && \\
			pod cache clean --all && \\
			rm -rf Pods && \\
			rm -rf Podfile.lock && \\
			rm -rf Unity-iPhone.xcworkspace && \\
			pod deintegrate && \\
			pod setup && \\
			pod update && \\
			pod repo update && \\
			pod install --repo-update")
	end tell
	delay 15
	addWidgetToProject()
end updatePod

on unpack()
	tell application "Terminal"
		close windows
	end tell

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
	--   IPHONEOS_DEPLOYMENT_TARGET=12.0 — silence old-target warnings from legacy pods
	--   ENABLE_USER_SCRIPT_SANDBOXING=NO — Xcode 15+ sandboxing breaks gRPC-Core
	--     libtool step with "Command Libtool failed with a nonzero exit code";
	--     gRPC's header-symlink script phase can't write inside the sandbox.
	--   ALWAYS_OUT_OF_DATE=YES on script phases — silences Xcode's
	--     "Run script build phase will be run during every build" warnings
	--     emitted for Unity's GameAssembly + gRPC/BoringSSL/abseil.
	-- Versioned marker ("# ubd-post-install v2") lets us re-patch when we
	-- add settings in a newer version without duplicating the block.
	set podfilePatch to "
# ubd-post-install v3
post_install do |installer|
  installer.pods_project.targets.each do |target|
    target.build_configurations.each do |config|
      config.build_settings['IPHONEOS_DEPLOYMENT_TARGET'] = '12.0'
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

	tell application "Terminal"
		activate
		do script my nccmd("cd {{WORK_DIR}}/iOS && pod install")
	end tell
	delay 25

	tell application "Terminal"
		close windows
	end tell

	addWidgetToProject()
	delay 20
end unpack

-- Build + test: runs the app through xctest, which auto-launches it on device.
-- Works for most Unity projects; some crash during test-runner init
-- (NSInvalidArgumentException in UIApplicationMain). If that happens, use
-- `installDevice` mode instead (Settings → iOS → "Run mode: Install only").
on runDevice(deviceName)
	stopTerminal()
	tell application "Terminal"
		do script my nccmd("cd {{WORK_DIR}}/iOS && xcodebuild -workspace Unity-iPhone.xcworkspace -scheme Unity-iPhone -destination 'platform=iOS,name=" & deviceName & "' -allowProvisioningUpdates test")
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
	set cmd to "cd {{WORK_DIR}}/iOS && xcodebuild -workspace Unity-iPhone.xcworkspace -scheme Unity-iPhone -configuration Debug -destination 'platform=iOS,name=" & deviceName & "' -allowProvisioningUpdates -derivedDataPath build/DerivedData build && APP_PATH=$(/usr/bin/find build/DerivedData/Build/Products -maxdepth 3 -type d -name '*.app' | /usr/bin/grep -v Tests | /usr/bin/head -1) && echo \"Installing $APP_PATH on " & deviceName & "\" && xcrun devicectl device install app --device '" & deviceName & "' \"$APP_PATH\""
	tell application "Terminal"
		do script my nccmd(cmd)
	end tell
end installDevice

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
	end if
end run
