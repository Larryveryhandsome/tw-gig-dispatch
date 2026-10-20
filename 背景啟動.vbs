' Start the dispatch site in background with pythonw (no console window). Log: data\dispatch.log
Option Explicit
Dim sh, fso, here, exe
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
If Not fso.FileExists(here & "\.env") Then
  MsgBox ".env not found. Copy .env.example to .env and fill it in (see README).", 48, "tw-gig-dispatch"
  WScript.Quit 1
End If
On Error Resume Next
For Each exe In Array("pythonw", "pyw")
  Err.Clear
  sh.Run exe & " -X utf8 ""app.py""", 0, False
  If Err.Number = 0 Then WScript.Quit 0
Next
MsgBox "Python 3.10+ not found. Install it from https://www.python.org/downloads/ (check Add python.exe to PATH).", 48, "tw-gig-dispatch"
