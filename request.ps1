# 1. Load your key + agent id from .env (so nothing secret is hardcoded)
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') {
        Set-Item -Path "env:$($matches[1].Trim())" -Value $matches[2].Trim()
    }
}

# 2. Ask the agent to describe itself
$headers = @{
    "X-ABBY-API-Key" = $env:ABBY_API_KEY
    "Content-Type"   = "application/json"
}

$body = @{
    agent_id = $env:ABBY_AGENT_ID
    input = @"
Please describe yourself and your configuration. Specifically answer:
1. What is your role and primary task?
2. What files are attached to your knowledge base (list every file name)?
3. Which underlying LLM model are you running on?
4. What output format do you produce (fields/structure)?
5. Do you have access to ABB Library & Policies tools? If yes, are they usable via the API?
6. How do you handle requirement SM-2 (Identification of Responsibility)?
Answer as plain text.
"@
} | ConvertTo-Json

$response = Invoke-RestMethod `
    -Uri "https://api.abby.abb.com/api/v1/developers/agent_chat" `
    -Method Post `
    -Headers $headers `
    -Body $body `
    -TimeoutSec 120

# 3. Show the result
$response | ConvertTo-Json -Depth 10
Write-Host "`n----- ASSISTANT TEXT -----`n"
$response.output.content