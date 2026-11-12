Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.Run """" & base & "\snowluma_start.bat""", 0, False