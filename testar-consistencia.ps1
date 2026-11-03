# Teste de consistencia do LLM no Docker (Windows).
# Reconstroi a API com o cache DESLIGADO, faz as perguntas de eval\perguntas.json varias vezes,
# confere as respostas e mostra o placar. Detalhes (uma linha JSON por resposta) em
# resultado-consistencia.txt. Uso: .\testar-consistencia.ps1 [-Rodadas 3]
# (Com Python instalado, scripts\avaliar.py faz a mesma avaliacao em qualquer sistema.)
param([int]$Rodadas = 3)
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$out = Join-Path $PSScriptRoot "resultado-consistencia.txt"
"" | Out-File $out -Encoding utf8 -NoNewline

$avaliacao = Get-Content (Join-Path $PSScriptRoot "eval\perguntas.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$perguntas = $avaliacao.perguntas
$proibido = @($avaliacao.proibido)

Write-Host "--- subindo todos os servicos (API reconstruida, cache desligado) ---"
$env:ANSWER_CACHE_TTL = "0"
docker compose up -d --build 2>&1 | Out-Null
Write-Host "--- aguardando o modelo (download automatico na 1a vez) ---"
docker compose up ollama-pull 2>&1 | Select-Object -Last 1 | ForEach-Object { Write-Host $_ }
$ok = $false
for ($i = 0; $i -lt 40; $i++) {
    try { Invoke-RestMethod "http://localhost:8000/health" -TimeoutSec 5 | Out-Null; $ok = $true; break }
    catch { Start-Sleep 3 }
}
if (-not $ok) { Write-Host "API nao respondeu"; Read-Host "Enter para fechar"; exit 1 }
$servicos = (docker compose ps -a 2>&1 | Out-String)
$saude = try { (Invoke-WebRequest "http://localhost:8000/health" -UseBasicParsing).Content } catch { "$_" }
(@{ servicos = $servicos; health = $saude } | ConvertTo-Json -Compress) | Out-File $out -Append -Encoding utf8

function Conferir($item, $status, $resposta) {
    if ($status -ne [int]$item.status) { return $false }
    if ($status -ne 200) { return $true }
    $texto = "$resposta".ToLower()
    foreach ($p in $proibido) { if ($p -and $texto.Contains($p.ToLower())) { return $false } }
    foreach ($trecho in $item.esperado) {
        $achou = $false
        foreach ($opcao in $trecho.Split("|")) { if ($texto.Contains($opcao.ToLower())) { $achou = $true } }
        if (-not $achou) { return $false }
    }
    return $true
}

$total = $Rodadas * $perguntas.Count
$n = 0; $acertos = 0
$respostas = @{}
$porGrupo = @{}
$inicio = Get-Date
for ($r = 1; $r -le $Rodadas; $r++) {
    foreach ($item in $perguntas) {
        $n++
        $p = $item.pergunta
        $url = "http://localhost:8000/sales-insights?question=$([uri]::EscapeDataString($p))"
        $t = Get-Date
        $status = 0; $body = ""; $resposta = ""
        try {
            $resp = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 600
            $status = [int]$resp.StatusCode
            $body = [System.Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
        } catch {
            if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $body = $_.ErrorDetails.Message }
            else { $body = $_.Exception.Message }
        }
        try { $json = $body | ConvertFrom-Json; $resposta = if ($json.answer) { $json.answer } else { $json.detail } } catch { $resposta = $body }
        $acertou = Conferir $item $status $resposta
        if ($acertou) { $acertos++ }
        $grupo = if ($item.grupo) { $item.grupo } else { "geral" }
        if (-not $porGrupo.ContainsKey($grupo)) { $porGrupo[$grupo] = @(0, 0) }
        $porGrupo[$grupo][1]++
        if ($acertou) { $porGrupo[$grupo][0]++ }
        if (-not $respostas.ContainsKey($p)) { $respostas[$p] = @{} }
        $respostas[$p]["$resposta"] = $true
        $segundos = [math]::Round(((Get-Date) - $t).TotalSeconds, 1)
        $marca = if ($acertou) { "OK  " } else { "ERRO" }
        Write-Host ("[{0}/{1}] {2} {3} {4,6}s  {5}" -f $n, $total, $marca, $status, $segundos, $p)
        if (-not $acertou) { Write-Host "           resposta: $resposta" }
        $linha = [ordered]@{
            rodada   = $r
            grupo    = $grupo
            pergunta = $p
            status   = $status
            acertou  = $acertou
            segundos = $segundos
            corpo    = $body
        }
        ($linha | ConvertTo-Json -Compress) | Out-File $out -Append -Encoding utf8
    }
}

Write-Host "`n--- conferindo que nenhuma venda foi apagada (deve ser 33) ---"
$vendas = docker compose exec -T db psql -U sales -d sales -t -A -c "SELECT COUNT(*) FROM sales" 2>&1
Write-Host $vendas
(@{ verificacao = "total_de_vendas_no_banco"; valor = "$vendas".Trim() } | ConvertTo-Json -Compress) | Out-File $out -Append -Encoding utf8

Write-Host "`n--- religando o cache ---"
Remove-Item Env:ANSWER_CACHE_TTL
docker compose up -d api 2>&1 | Out-Null

$instaveis = @($respostas.Keys | Where-Object { $respostas[$_].Count -gt 1 })
Write-Host "`n=== PLACAR ==="
Write-Host ("Acertos: {0}/{1} ({2:P0})" -f $acertos, $total, ($acertos / $total))
foreach ($g in ($porGrupo.Keys | Sort-Object)) {
    Write-Host ("  {0}: {1}/{2}" -f $g, $porGrupo[$g][0], $porGrupo[$g][1])
}
if ($Rodadas -gt 1) {
    Write-Host ("Perguntas com respostas diferentes entre rodadas: {0}" -f $instaveis.Count)
    foreach ($p in $instaveis) { Write-Host "  - $p" }
}
Write-Host ("Concluido em {0:N0} minutos." -f ((Get-Date) - $inicio).TotalMinutes)
(@{ fim = $true; acertos = $acertos; total = $total } | ConvertTo-Json -Compress) | Out-File $out -Append -Encoding utf8
Read-Host "Pressione Enter para fechar"
