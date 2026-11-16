Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set ws = CreateObject("Wscript.Shell")
ws.Run """" & dir & "\start.bat""", 0, False
