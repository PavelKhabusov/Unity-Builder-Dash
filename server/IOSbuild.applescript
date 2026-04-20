(*
 IOSbuild.applescript — source for IOSbuild.scpt (Unity Builder Dash iOS remote).

 Compiled into .scpt on the Mac by server/patch_scpt.sh after substituting
 the {{PLACEHOLDERS}} below with values from config.json. The host (Linux or
 Windows) scp's IOS.zip into {{WORK_DIR}}/IOS.zip before invoking a command.

 Placeholders (all rewritten by patch_scpt.sh from env vars):
   {{WORK_DIR}}          Mac-side base folder (scripts + build artefacts)
   {{WIDGET_FOLDER}}     Folder name next to IOS/ with widget sources
   {{WIDGET_BUNDLE_ID}}  Widget CFBundleIdentifier
   {{WIDGET_TEAM_ID}}    Apple Developer Team ID for widget signing
   {{WIDGET_TARGET}}     Xcode target name for the widget (e.g. URLImageWidget)
   {{APP_GROUP_ID}}      App Group ID shared between app and widget
   {{SMB_USER}}          Windows SMB user (optional, Windows host only)
   {{SMB_PASS}}          Windows SMB password (optional, Windows host only)
   {{SMB_BUILD_PATH}}    Relative path on SMB share to build/IOS.zip parent

 Commands (argv[0]):
   run:<device>              stopTerminal + xcodebuild-test on <device>
   runFull:<device>          unpack + run:<device>
   unpack                    unzip IOS.zip, pod install, add widget
   stop                      kill active Terminal job
   clearCache                remove Xcode DerivedData / .pcm
   clearBuild                xcodebuild clean
   updatePod                 full pod reinstall + add widget
   addWidget                 re-run add_widget_dependency.rb
   connectMac-<winIp>-<mac>  (Windows host only) SMB-mount winIp, save winIp
                             into config.json. Linux hosts don't need this
                             since they scp IOS.zip directly.
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
	tell application "Terminal"
		if (count of windows) > 0 then
			set activeWindow to front window
			set activeTab to selected tab of activeWindow
			tell activeTab to do script "kill $(jobs -p); exit" in activeTab
			repeat until (busy of activeTab is false)
				delay 0.2
			end repeat
			close activeWindow
		end if
	end tell
end stopTerminal

on clearCache()
	set pcmFolder to (path to library folder from user domain as text) & "Developer:Xcode:DerivedData:ModuleCache.noindex"
	tell application "Finder"
		set pcmFiles to (every file of folder pcmFolder whose name ends with ".pcm")
		repeat with aFile in pcmFiles
			delete aFile
		end repeat
		set derivedDataFolder to (path to library folder from user domain as text) & "Developer:Xcode:DerivedData"
		set unityFolders to (every folder of folder derivedDataFolder whose name contains "Unity-iPhone")
		repeat with unityFolder in unityFolders
			delete unityFolder
		end repeat
	end tell
end clearCache

on clearBuild()
	tell application "Terminal"
		do script my nccmd("cd {{WORK_DIR}}/IOS && xcodebuild clean && rm -rf ./build/Build")
	end tell
end clearBuild

on addWidgetToProject()
	set widgetSource to "{{WORK_DIR}}/{{WIDGET_FOLDER}}/Widgets/"
	set projectPath to "{{WORK_DIR}}/IOS/"
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

	set terminalCommand to "
/usr/libexec/PlistBuddy -c 'Delete :com.apple.security.application-groups' " & entitlementsFile & " || echo 'Skip delete';
/usr/libexec/PlistBuddy -c 'Add :com.apple.security.application-groups array' " & entitlementsFile & ";
/usr/libexec/PlistBuddy -c 'Add :com.apple.security.application-groups:0 string " & appGroupID & "' " & entitlementsFile & ";
/usr/libexec/PlistBuddy -c 'Delete :com.apple.security.application-groups' " & widgetPlist & " || echo 'Skip delete';
/usr/libexec/PlistBuddy -c 'Add :com.apple.security.application-groups array' " & widgetPlist & ";
/usr/libexec/PlistBuddy -c 'Add :com.apple.security.application-groups:0 string " & appGroupID & "' " & widgetPlist & ";
/usr/libexec/PlistBuddy -c 'Add :CFBundleIdentifier string {{WIDGET_BUNDLE_ID}}' " & widgetPlist & ";
cd {{WORK_DIR}}/IOS;
gem install xcodeproj --no-document;
WIDGET_BUNDLE_ID='{{WIDGET_BUNDLE_ID}}' WIDGET_TEAM_ID='{{WIDGET_TEAM_ID}}' WIDGET_TARGET_NAME='{{WIDGET_TARGET}}' ruby {{WORK_DIR}}/add_widget_dependency.rb;
"
	tell application "Terminal"
		do script my nccmd(terminalCommand)
	end tell
end addWidgetToProject

on updatePod()
	tell application "Terminal"
		activate
		do script my nccmd("cd {{WORK_DIR}}/IOS && \\
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

	-- Delete old IOS/ directory if present (shell is reliable with missing paths)
	do shell script "rm -rf " & quoted form of "{{WORK_DIR}}/IOS"

	-- Empty trash (best-effort)
	try
		tell application "Finder"
			if (count of items in trash) > 0 then empty the trash
		end tell
	end try

	-- If local IOS.zip is missing but SMB mount has it, copy over. Silent fallback.
	try
		do shell script "test -f " & quoted form of "{{WORK_DIR}}/IOS.zip"
	on error
		try
			do shell script "cp /Volumes/Users/{{SMB_BUILD_PATH}}/IOS.zip " & quoted form of "{{WORK_DIR}}/IOS.zip"
		end try
	end try

	-- Verify zip is here before unzipping
	try
		do shell script "test -f " & quoted form of "{{WORK_DIR}}/IOS.zip"
	on error
		display dialog "Error: IOS.zip not found in {{WORK_DIR}}. Did the host SCP fail?" buttons {"OK"} default button "OK"
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
		set unzipLog to (do shell script "cd " & quoted form of "{{WORK_DIR}}" & " && unzip -o IOS.zip 2>&1")
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

	tell application "Terminal"
		activate
		do script my nccmd("cd {{WORK_DIR}}/IOS && pod install")
	end tell
	delay 25

	tell application "Terminal"
		close windows
	end tell

	addWidgetToProject()
	delay 20
end unpack

on runDevice(deviceName)
	stopTerminal()
	tell application "Terminal"
		do script my nccmd("cd {{WORK_DIR}}/IOS && xcodebuild -workspace Unity-iPhone.xcworkspace -scheme Unity-iPhone -destination 'platform=iOS,name=" & deviceName & "' test")
	end tell
end runDevice

-- ── Windows-host legacy: SMB mount for reading IOS.zip from a shared folder ──
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
