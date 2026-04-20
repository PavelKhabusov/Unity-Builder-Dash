(*
 mac_console.applescript — native macOS GUI console for ios_build.scpt.

 Compiled to mac_console.app by patch_scpt.sh at install time. Lives next to
 ios_build.scpt in the Mac work folder. Double-click the .app to open a
 native `choose from list` panel showing live status and action shortcuts.

 Zero extra dependencies — uses macOS's built-in AppleScript runtime.
*)

on getWorkDir()
	set myPath to POSIX path of (path to me)
	return (do shell script "dirname " & quoted form of myPath)
end getWorkDir

on cfgValue(key)
	set w to my getWorkDir()
	set py to "import json; d=json.load(open('" & w & "/config.json')); print(d.get('" & key & "',''))"
	try
		return (do shell script "python3 -c " & quoted form of py)
	on error
		return ""
	end try
end cfgValue

-- Let Python read config.json per call. Avoids all AppleScript list/paragraph
-- parsing quirks with text items / references / list concat. One shell call
-- per lookup is slow in principle but imperceptible for a menu with a few
-- devices.

on cfgDeviceCount()
	set w to my getWorkDir()
	try
		return (do shell script "python3 -c 'import json; print(len(json.load(open(\"" & w & "/config.json\")).get(\"devices\",[])))'") as integer
	on error
		return 0
	end try
end cfgDeviceCount

on cfgDeviceField(i, fieldName)
	set w to my getWorkDir()
	set idx to (i - 1) as text
	set py to "import json
d=json.load(open('" & w & "/config.json')).get('devices',[])
i=" & idx & "
if 0<=i<len(d):
    r=d[i]
    print(r.get('" & fieldName & "','') or r.get('name',''))"
	try
		return (do shell script "python3 -c " & quoted form of py)
	on error
		return ""
	end try
end cfgDeviceField

on statusLine()
	set w to my getWorkDir()
	set hostIP to my cfgValue("host_ip")
	if hostIP is "" then set hostIP to "(not set)"

	set iosInfo to "(not unpacked)"
	try
		set sizeStr to (do shell script "du -sh " & quoted form of (w & "/iOS") & " 2>/dev/null | cut -f1")
		if sizeStr is not "" then set iosInfo to sizeStr
	end try

	set zipInfo to "(missing)"
	try
		set zipSize to (do shell script "du -h " & quoted form of (w & "/iOS.zip") & " 2>/dev/null | cut -f1")
		if zipSize is not "" then set zipInfo to zipSize
	end try

	return "📁 " & w & return & ¬
		"🌐 Host IP: " & hostIP & "    📦 iOS/: " & iosInfo & "    🗜 iOS.zip: " & zipInfo
end statusLine

-- Fire-and-forget: fork osascript in the background so the console stays
-- responsive (Cmd+Q, Dock Quit, re-opening the menu). Output/errors go to
-- $WORK_DIR/mac_console.log for post-mortem.
on runAction(cmd)
	set w to my getWorkDir()
	set scpt to w & "/ios_build.scpt"
	set logFile to w & "/mac_console.log"
	try
		do shell script "nohup osascript " & quoted form of scpt & " " & quoted form of cmd & " >> " & quoted form of logFile & " 2>&1 &"
	on error errMsg
		display alert "Action failed: " & cmd message errMsg as warning
	end try
end runAction

-- Pick a device: first try the list from config.json, else free-form input.
on askDevice()
	set n to my cfgDeviceCount()
	if n > 0 then
		-- Build display-name list one shell call at a time. Reliable and
		-- fast enough for a menu of <20 devices.
		set displayList to {}
		repeat with i from 1 to n
			set d to my cfgDeviceField(i, "display_name")
			set end of displayList to d
		end repeat

		set picked to (choose from list displayList with title "Select device" with prompt "Device for xcodebuild -destination:" default items {item 1 of displayList} OK button name "Run" cancel button name "Cancel")
		if picked is false then return ""
		set chosenDisplay to (item 1 of picked) as string
		repeat with i from 1 to n
			if (my cfgDeviceField(i, "display_name")) is chosenDisplay then
				return my cfgDeviceField(i, "name")
			end if
		end repeat
		return chosenDisplay
	else
		try
			set reply to display dialog "Device name (matches xcodebuild -destination 'platform=iOS,name=...'):" default answer "iPhone 12 mini" with title "Unity Builder Dash — Mac Console" buttons {"Cancel", "Run"} default button "Run"
			if button returned of reply is "Cancel" then return ""
			return text returned of reply
		on error
			return ""
		end try
	end if
end askDevice

on editIP()
	set w to my getWorkDir()
	set current to my cfgValue("host_ip")
	try
		set reply to display dialog "Host IP — where the Mac sends progress (via nc):" default answer current with title "Edit host IP" buttons {"Cancel", "Save"} default button "Save"
		if button returned of reply is "Save" then
			set newIP to text returned of reply
			-- Update host_ip in config.json (preserves everything else).
			set py to "import json
p='" & w & "/config.json'
try: d=json.load(open(p))
except: d={}
d['host_ip']='" & newIP & "'
open(p,'w').write(json.dumps(d,indent=2))"
			do shell script "python3 -c " & quoted form of py
		end if
	end try
end editIP

on openWorkDir()
	tell application "Finder" to open folder (POSIX file (my getWorkDir()))
end openWorkDir

on dispatch(pick)
	if pick contains "Unpack" then
		my runAction("unpack")
	else if pick contains "Run on device" then
		set dev to my askDevice()
		if dev is not "" then my runAction("run:" & dev)
	else if pick contains "Full (unpack" then
		set dev to my askDevice()
		if dev is not "" then my runAction("runFull:" & dev)
	else if pick contains "Clear .pcm" then
		my runAction("clearCache")
	else if pick contains "Clean build" then
		my runAction("clearBuild")
	else if pick contains "Add widget" then
		my runAction("addWidget")
	else if pick contains "Update Pod" then
		my runAction("updatePod")
	else if pick contains "Stop active" then
		my runAction("stop")
	else if pick contains "Open work folder" then
		my openWorkDir()
	else if pick contains "Edit host IP" then
		my editIP()
	end if
end dispatch

-- Required so Cmd+Q / Dock → Quit terminate the app cleanly. Without this
-- handler, stay-open AppleScript apps refuse to quit when a modal is up.
on quit
	continue quit
end quit

on run
	-- Section headers are pure decoration. Any pick starting with "━"
	-- is ignored (list reopens) so they behave as disabled dividers.
	set actions to {¬
		"━━━━━━  ARCHIVE  ━━━━━━", ¬
		"   📦  Unpack", ¬
		"━━━━━━  BUILD  ━━━━━━", ¬
		"   ▶️  Run on device…", ¬
		"   🎬  Full (unpack + run)…", ¬
		"━━━━━━  EXTRAS  ━━━━━━", ¬
		"   🧹  Clear .pcm cache", ¬
		"   🧼  Clean build", ¬
		"   🧩  Add widget", ¬
		"   🔄  Update Pod", ¬
		"━━━━━━  TOOLS  ━━━━━━", ¬
		"   ⏹  Stop active Terminal", ¬
		"   📁  Open work folder", ¬
		"   🌐  Edit host IP"}

	repeat
		set picked to (choose from list actions with title "Unity Builder Dash — Mac Console" with prompt (my statusLine()) OK button name "Run" cancel button name "Quit")
		if picked is false then return
		set pick to item 1 of picked
		-- Ignore clicks on decorative dividers
		if pick starts with "━" then
			-- loop and reopen
		else
			my dispatch(pick)
		end if
	end repeat
end run
