Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & base & "\watchdog.ps1""", 0, False