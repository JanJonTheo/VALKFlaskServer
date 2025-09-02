# PowerShell setup script for Windows

Write-Host "🔍 Checking Docker setup..." -ForegroundColor Green

# Check if Docker Compose is available
if (!(Get-Command docker-compose -ErrorAction SilentlyContinue)) {
    Write-Host "❌ docker-compose not found. Please install Docker Compose." -ForegroundColor Red
    exit 1
}

Write-Host "✅ Docker Compose found." -ForegroundColor Green

# Check if .env files exist
if (!(Test-Path "VALKFlaskServer\.env")) {
    Write-Host "⚠️  VALKFlaskServer\.env not found. Please create it from the template." -ForegroundColor Yellow
}

if (!(Test-Path "VALKStreamlitDashboard\.env")) {
    Write-Host "⚠️  VALKStreamlitDashboard\.env not found. Please create it from the template." -ForegroundColor Yellow
}

Write-Host "🚀 Starting services..." -ForegroundColor Green
docker-compose up -d

Write-Host "⏳ Waiting for services to start..." -ForegroundColor Yellow
Start-Sleep 10

Write-Host "📊 Checking service status..." -ForegroundColor Green
docker-compose ps

Write-Host "📱 Services should be available at:" -ForegroundColor Cyan
Write-Host "   Flask API: http://localhost:5000" -ForegroundColor White
Write-Host "   Streamlit Dashboard: http://localhost:8501" -ForegroundColor White

Write-Host "🔍 To check logs:" -ForegroundColor Cyan
Write-Host "   docker-compose logs flaskserver" -ForegroundColor White
Write-Host "   docker-compose logs streamlitdashboard" -ForegroundColor White

Write-Host "🛑 To stop services:" -ForegroundColor Cyan
Write-Host "   docker-compose down" -ForegroundColor White
