' Stop the background dispatch site using data\dispatch.pid (hidden, no console window)
Option Explicit
Dim sh, fso, here, pidFile, pid
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
pidFile = here & "\data\dispatch.pid"
If Not fso.FileExists(pidFile) Then
  WScript.Quit 0
End If
pid = Trim(fso.OpenTextFile(pidFile, 1).ReadAll)
sh.Run "taskkill /PID " & pid & " /T /F", 0, True
fso.DeleteFile pidFile
