Write-Host "Запуск ngrok туннеля на порт 5000..."
Write-Host "Скопируй HTTPS-ссылку (https://xxxx.ngrok-free.app) и вставь её в настройках VK:"
Write-Host "   Управление сообществом → Работа с API → Callback API → Адрес сервера"
Write-Host ""

$ngrok = "ngrok"

& $ngrok http 5000

if (-not $?) {
    Write-Host "ngrok не найден. Установи: https://ngrok.com/download"
    Write-Host "   Или используй: winget install ngrok"
}
