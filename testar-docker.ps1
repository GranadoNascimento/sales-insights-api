# Sobe o projeto no Docker, baixa o modelo e testa os endpoints.
# Resultado salvo em resultado-docker.txt (nesta mesma pasta).
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$out = Join-Path $PSScriptRoot "resultado-docker.txt"
"=== Teste iniciado em $(Get-Date) ===" | Out-File $out -Encoding utf8

function Log($msg) { Write-Host $msg; $msg | Out-File $out -Append -Encoding utf8 }

Log "`n--- subindo banco e Ollama ---"
docker compose up -d --wait db ollama 2>&1 | ForEach-Object { Log "$_" }

# O Postgres so executa os scripts de db/ na primeira subida. Se o banco ja existia,
# aplica o script do usuario somente leitura (ele pode rodar varias vezes sem erro).
Log "`n--- usuario somente leitura (sales_reader) ---"
docker compose exec -T db psql -U sales -d sales -v ON_ERROR_STOP=1 -q -f /docker-entrypoint-initdb.d/02_readonly_user.sql 2>&1 | ForEach-Object { Log "$_" }
if ($LASTEXITCODE -eq 0) { Log "ok" } else { Log "FALHOU ao criar o usuario somente leitura" }

Log "`n--- baixando o modelo (so na primeira vez, ~4,7 GB) ---"
docker compose up ollama-pull 2>&1 | Select-Object -Last 3 | ForEach-Object { Log "$_" }

Log "`n--- construindo e subindo a API ---"
docker compose up -d --build api 2>&1 | Select-Object -Last 8 | ForEach-Object { Log "$_" }

Log "`n--- aguardando a API ---"
$ok = $false
for ($i = 0; $i -lt 40; $i++) {
    try { Invoke-RestMethod "http://localhost:8000/health" -TimeoutSec 5 | Out-Null; $ok = $true; break }
    catch { Start-Sleep 3 }
}
if (-not $ok) {
    Log "API nao respondeu. Logs:"
    docker compose logs --tail 60 api 2>&1 | ForEach-Object { Log "$_" }
    Read-Host "Pressione Enter para fechar"; exit 1
}

function Testar($nome, $url) {
    Log "`n### $nome"
    Log "GET $url"
    $t = Get-Date
    try {
        $r = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 600
        Log "Status: $($r.StatusCode)"
        Log ([System.Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()))
    } catch {
        $resp = $_.Exception.Response
        if ($resp) {
            Log "Status: $([int]$resp.StatusCode)"
            # No PowerShell 5.1 o corpo da resposta de erro fica em ErrorDetails.Message
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                Log $_.ErrorDetails.Message
            } else {
                try {
                    $stream = $resp.GetResponseStream()
                    if ($stream.CanSeek) { $stream.Position = 0 }
                    $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
                    Log $reader.ReadToEnd()
                } catch { Log "(nao foi possivel ler o corpo da resposta)" }
            }
        } else { Log "Erro: $($_.Exception.Message)" }
    }
    Log ("Tempo: {0:N1}s" -f ((Get-Date) - $t).TotalSeconds)
}

$base = "http://localhost:8000"
Testar "health" "$base/health"
Testar "top-products" "$base/top-products"
$perguntas = @(
    "Qual foi o produto mais vendido na última semana?",
    "Qual cliente mais gastou em fevereiro?",
    "Qual o faturamento total por categoria?",
    "Quantas vendas foram feitas em janeiro?",
    "Quem gastou mais em fevereiro?",
    "Qual foi o melhor mês em faturamento?",
    "Qual é a capital da França?",
    "Qual foi o produto mais vendido na última semana?"
)
foreach ($p in $perguntas) {
    Testar $p "$base/sales-insights?question=$([uri]::EscapeDataString($p))"
}

Log "`n--- usuario da API no banco (deve ser sales_reader) ---"
docker compose exec -T db psql -U sales -d sales -t -c "SELECT usename, count(*) FROM pg_stat_activity WHERE datname = 'sales' AND usename IS NOT NULL GROUP BY usename" 2>&1 | ForEach-Object { Log "$_" }

Log "`n--- container da API roda como (deve ser appuser) ---"
docker compose exec -T api whoami 2>&1 | ForEach-Object { Log "$_" }

Log "`n--- logs da API (ultimas 40 linhas) ---"
docker compose logs --tail 40 api 2>&1 | ForEach-Object { Log "$_" }
Log "`n=== FIM ==="
Read-Host "Concluido. Pressione Enter para fechar"
