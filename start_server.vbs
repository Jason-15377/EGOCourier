' EGO Courier - 无控制台窗口启动（双击本文件）
' 服务在后台运行，浏览器自动打开；关闭方式见下方提示。
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
' 以脚本所在目录为工作目录（目录改名/移动后依然可用）
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
' 后台启动服务（无窗口）
sh.Run """python"" -m ego_relay.api --port 8360", 0, False
' 稍等后打开浏览器
WScript.Sleep 1500
sh.Run "http://127.0.0.1:8360", 1, False
