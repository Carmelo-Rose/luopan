Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.Run Chr(34) & scriptDir & "\run_multi_then_acc.bat" & Chr(34), 0, True
