#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

nginx_root="$tmp_dir/nginx"
bin_dir="$tmp_dir/bin"
mkdir -p "$nginx_root"/{sites-available,sites-enabled,conf.d} "$bin_dir"

cat > "$bin_dir/id" <<'SH'
#!/usr/bin/env bash
if [ "${1:-}" = "-u" ]; then
  printf '1000\n'
else
  /usr/bin/id "$@"
fi
SH
chmod +x "$bin_dir/id"

cat > "$bin_dir/sudo" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${SUDO_LOG:?}"
exec "$@"
SH
chmod +x "$bin_dir/sudo"

cat > "$bin_dir/nginx" <<'SH'
#!/usr/bin/env bash
[ "${1:-}" = "-t" ]
SH
chmod +x "$bin_dir/nginx"

cat > "$bin_dir/systemctl" <<'SH'
#!/usr/bin/env bash
[ "${1:-}" = "reload" ] && [ "${2:-}" = "nginx" ]
SH
chmod +x "$bin_dir/systemctl"

export SUDO_LOG="$tmp_dir/sudo.log"
PATH="$bin_dir:$PATH" \
NGINX_ETC_DIR="$nginx_root" \
APP_DOMAIN=app.example.test \
API_DOMAIN=api.example.test \
  bash "$repo_root/deploy/setup-nginx.sh" > "$tmp_dir/setup.log" || {
  cat "$tmp_dir/setup.log"
  exit 1
}

for path in \
  "$nginx_root/conf.d/wrzdj-logging.conf" \
  "$nginx_root/conf.d/wrzdj-tuning.conf" \
  "$nginx_root/sites-available/api.example.test" \
  "$nginx_root/sites-available/app.example.test" \
  "$nginx_root/sites-available/default"; do
  test -f "$path"
done

test -L "$nginx_root/sites-enabled/api.example.test"
test -L "$nginx_root/sites-enabled/app.example.test"
test -L "$nginx_root/sites-enabled/default"

grep -Fq '/etc/letsencrypt/live/api.example.test/fullchain.pem' \
  "$nginx_root/sites-available/api.example.test"
grep -Fq '/etc/letsencrypt/live/api.example.test-rsa/fullchain.pem' \
  "$nginx_root/sites-available/api.example.test"
grep -Fq '/etc/letsencrypt/live/app.example.test-rsa/fullchain.pem' \
  "$nginx_root/sites-available/app.example.test"
grep -Fq 'cp ' "$SUDO_LOG"
grep -Fq 'ln ' "$SUDO_LOG"

grep -Fq -- '--cert-name api.yourdomain.com --key-type ecdsa -d api.yourdomain.com' \
  "$repo_root/deploy/DEPLOYMENT.md"
grep -Fq -- '--cert-name api.yourdomain.com-rsa --key-type rsa -d api.yourdomain.com' \
  "$repo_root/deploy/DEPLOYMENT.md"
grep -Fq "certbot_args+=(\"--cert-name\" \"\$cert_name\")" \
  "$repo_root/deploy/setup-user.sh"
grep -Fq "certbot_args+=(\"--key-type\" \"\$key_type\")" \
  "$repo_root/deploy/setup-user.sh"
grep -Fq 'wrzdj-certbot --nginx --cert-name api.example.test-rsa --key-type rsa -d api.example.test' \
  "$tmp_dir/setup.log"

printf 'setup-nginx provisioning contract passed\n'
