# Implantação do OrbitBase na VPS (Hostinger KVM 1)

Destino: **https://orbitbase.biomereurb.com.br**, isolado do site biomereurb.com.br que já roda na mesma VPS.

## Como fica

```
Internet ──443──> servidor web do host (nginx ou apache, o mesmo do biomereurb)
                     ├─ biomereurb.com.br          → site existente (não é alterado)
                     └─ orbitbase.biomereurb.com.br → 127.0.0.1:8088
                                                        └─ container web (nginx: frontend + /api)
                                                             └─ container backend (FastAPI)
```

* O OrbitBase roda em dois containers Docker que só escutam em `127.0.0.1:8088`.
* O servidor web do host ganha **um arquivo novo** (`orbitbase.conf`) com `server_name orbitbase.biomereurb.com.br`; nenhum arquivo do biomereurb é editado.
* HTTPS via Let's Encrypt (certbot) apenas para o subdomínio.
* O frontend chama a API em `/api` na mesma origem; localmente continua usando `http://127.0.0.1:8000`.

## Impacto na VPS (1 vCPU, 4 GB RAM, 50 GB)

| Recurso | Limite do OrbitBase | Observação |
|---|---|---|
| CPU | 0,75 vCPU no máximo, prioridade menor (`cpu_shares: 512`) | Em processamento pesado o biomereurb continua com prioridade de CPU |
| RAM | backend 2,6 GB (+1 GB de swap) + web 96 MB | Em repouso usa ~80 MB. Se um job passar do limite, só o processo do OrbitBase é encerrado e o job é marcado como falho |
| Disco | imagem ~250 MB + `deploy/data/` | RINEX, efemérides e resultados crescem com o uso; limpe `deploy/data/storage/processing` periodicamente |
| Rede | — | Downloads de RBMC/IBGE e NASA CDDIS durante os jobs |

Ajuste os limites em `deploy/.env` (`ORBITBASE_CPUS`, `ORBITBASE_MEM`) e rode `docker compose up -d` em `deploy/`.

## Passo 1: DNS

No painel onde o DNS de biomereurb.com.br é gerenciado (hPanel → Domínios → DNS, ou registro.br se os nameservers estiverem lá), crie:

| Tipo | Nome | Aponta para | TTL |
|---|---|---|---|
| A | `orbitbase` | `2.25.204.117` | 3600 |

Confira com `getent hosts orbitbase.biomereurb.com.br` na VPS.

## Passo 2: instalar

No console web da Hostinger (ou por SSH), como root:

```bash
# só diagnóstico, não altera nada
curl -fsSL https://raw.githubusercontent.com/Potyguara/orbitbase/main/deploy/install.sh | MODE=check bash

# instalação
curl -fsSL https://raw.githubusercontent.com/Potyguara/orbitbase/main/deploy/install.sh | \
  DOMAIN=orbitbase.biomereurb.com.br EMAIL=seu-email@exemplo.com bash
```

O script é idempotente (pode rodar de novo para atualizar) e:

1. Mostra um diagnóstico (servidor web nas portas 80/443, memória, disco, Docker, DNS).
2. Salva a configuração atual de `/etc/nginx`, `/etc/apache2` e `/etc/letsencrypt` em `/root/orbitbase-backups/<data>/`.
3. Instala Docker e git se faltarem.
4. Clona o repositório em `/opt/orbitbase`, cria `deploy/.env` com `SECRET_KEY` aleatória e sobe os containers.
5. Cria o vhost do subdomínio no nginx ou apache do host. Se o teste de configuração (`nginx -t` / `apachectl configtest`) falhar, remove o arquivo novo e para, sem recarregar o servidor.
6. Emite o certificado HTTPS se o DNS já aponta para a VPS (senão, rode de novo depois da propagação).

Servidores suportados automaticamente: nginx, apache, nenhum (instala nginx) ou **Caddy em container** (caso atual da VPS do biomereurb).

No modo Caddy, o script conecta o container `web` do OrbitBase à rede Docker do Caddy (`docker-compose.caddy.yml`, alias `orbitbase-web`), acrescenta ao Caddyfile existente um bloco entre `# >>> orbitbase` e `# <<< orbitbase`, roda `caddy validate` (se falhar, restaura o arquivo e não recarrega) e faz `caddy reload`, que não derruba o site. O HTTPS do subdomínio é emitido pelo próprio Caddy; certbot não é usado. O rollback remove só esse bloco e recarrega o Caddy. Para OpenLiteSpeed/CyberPanel, Traefik/Coolify/Easypanel ou outro proxy em container, o script para antes de mexer em qualquer coisa; nesse caso aponte o proxy existente para `http://127.0.0.1:8088`.

## Credenciais Earthdata

Cada usuário pode cadastrar as próprias no perfil. Para ter uma credencial padrão do servidor, preencha em `/opt/orbitbase/deploy/.env`:

```
EARTHDATA_USERNAME=...
EARTHDATA_PASSWORD=...
```

e rode `cd /opt/orbitbase/deploy && docker compose up -d`.

## Operação

```bash
cd /opt/orbitbase/deploy
docker compose ps                 # estado
docker compose logs -f backend    # logs
docker stats --no-stream          # consumo
BRANCH=main bash install.sh       # atualizar para a versão mais nova
```

Dados persistentes: `deploy/data/db/orbitbase.db` (SQLite) e `deploy/data/storage/`.

## Contas de usuário

O cadastro pelo site está desligado: quem quer acesso usa a página "Solicitar cotação"
(`contato.html`) e o administrador cria a conta na VPS. As contas que já existiam continuam valendo.

```bash
cd /opt/orbitbase/deploy
docker compose exec backend python -m app.manage_users list
docker compose exec -it backend python -m app.manage_users create --name "Fulano" --email fulano@exemplo.com   # pede a senha
docker compose exec -it backend python -m app.manage_users set-password --email fulano@exemplo.com
docker compose exec backend python -m app.manage_users deactivate --email fulano@exemplo.com   # bloqueia (activate desfaz)
```

`--plan free|pro|premium` escolhe o plano na criação. Para reabrir o autocadastro, `ALLOW_SELF_REGISTRATION=true` no `.env`.

## Rollback

```bash
bash /opt/orbitbase/deploy/rollback.sh          # para containers e remove o vhost; mantém dados
bash /opt/orbitbase/deploy/rollback.sh --purge  # também apaga /opt/orbitbase e o certificado
```

O rollback só remove arquivos `orbitbase*.conf`; o biomereurb não é tocado. Em último caso, a configuração anterior está em `/root/orbitbase-backups/`.

## Acesso SSH

Nunca use ou compartilhe a senha root. Para acesso remoto, adicione uma chave pública em hPanel → VPS → Chave SSH.

## Backup e swap

* `deploy/backup.sh` roda todo dia às 03:30 (`/etc/cron.d/orbitbase-backup`, log em `/var/log/orbitbase-backup.log`): copia o SQLite para `/root/orbitbase-backups/db/` (mantém 14 dias) e apaga pastas de `storage/processing` paradas há mais de 30 dias.
* Restaurar: `cd /opt/orbitbase/deploy && docker compose stop backend && gunzip -c /root/orbitbase-backups/db/<arquivo>.db.gz > data/db/orbitbase.db && chown 1000:1000 data/db/orbitbase.db && docker compose start backend`.
* Swap de 2 GB em `/swapfile` (em `/etc/fstab`), com `vm.swappiness=10`.
