$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

Start-Process -FilePath "python" `
  -ArgumentList @("-u", "telegram_control_bot.py", "--announce") `
  -WorkingDirectory $ProjectRoot `
  -RedirectStandardOutput (Join-Path $ProjectRoot "telegram_control_bot.out.log") `
  -RedirectStandardError (Join-Path $ProjectRoot "telegram_control_bot.err.log") `
  -WindowStyle Hidden

Write-Host "Telegram control bot start requested."
