set -e

CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "✅ Installing uv (Python package manager)"
if ! command -v uv &> /dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
else
    echo "✅ uv is already installed. Updating to latest version."
    uv self update
fi

echo "✅ Installing project dependencies with uv"
uv sync

service_name=$(uv run config --project-name)
service_port=$(uv run config --flask-port)

echo "📋 Configuration:"
{
    uv run config --all | while IFS='=' read -r key value; do
        echo -e "   ${CYAN}${key}${NC}|${YELLOW}${value}${NC}"
    done
    echo -e "   ${CYAN}cloudflare_domain${NC}|${YELLOW}${service_name}.mnalavadi.org${NC}"
} | column -t -s '|'

echo "✅ Copying service files to systemd directory"
# Every unit in install/, not just the web one, so the backup scheduler gets
# installed too. deploy.py discovers the same set the same way.
units=$(cd install && ls *.service)
for unit in $units; do
    sudo cp "install/${unit}" "/lib/systemd/system/${unit}"
    sudo chmod 644 "/lib/systemd/system/${unit}"
    echo "   installed ${unit}"
done

echo "✅ Reloading systemd daemon"
sudo systemctl daemon-reload
sudo systemctl daemon-reexec

for unit in $units; do
    echo "✅ Enabling the service: ${unit}"
    sudo systemctl enable "${unit}"
    sudo systemctl restart "${unit}"
    sudo systemctl status "${unit}" --no-pager
done

echo "✅ Adding Cloudflared service"
/home/mnalavadi/add_cloudflared_service.sh ${service_name}.mnalavadi.org $service_port
echo "✅ Configuring Cloudflared DNS route"
cloudflared tunnel route dns raspberrypi-tunnel ${service_name}.mnalavadi.org
echo "✅ Restarting Cloudflared service"
sudo systemctl restart cloudflared

echo "✅ Setup completed successfully! 🎉"
