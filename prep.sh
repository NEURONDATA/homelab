
echo "✅ User $username created and SSH hardened successfully."

cp n8n/example.env n8n/.env
cp watchtower/example.env watchtower/.env
cp caddy/caddyfile/Caddyfile.example caddy/caddyfile/Caddyfile
cd ~
mv homelab /home/$username/homelab
chown -R $username:$username /home/$username/homelab

mkdir /home/$username/.config
chown -R $username:$username /home/$username/.config
