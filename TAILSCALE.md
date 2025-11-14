# Tailscale Setup for Video Download Service

This guide explains how to set up Tailscale to securely access your video download service over your private Tailscale network.

## Prerequisites

- Docker and Docker Compose installed
- Tailscale account (sign up at https://tailscale.com/)
- Tailscale auth key (generate at https://login.tailscale.com/admin/settings/keys)

## Setup Instructions

### 1. Create Environment File

Copy the example environment file and add your Tailscale auth key:

```bash
cp .env.example .env
```

Edit `.env` and replace the placeholder with your actual Tailscale auth key:

```bash
TS_AUTHKEY=tskey-auth-XXXXXXXXXXXXXXXX-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

**Important:** Never commit the `.env` file to git. It's already in `.gitignore`.

### 2. Generate Tailscale Auth Key

1. Go to https://login.tailscale.com/admin/settings/keys
2. Click "Generate auth key"
3. Configure the key:
   - **Reusable:** Yes (recommended for containers)
   - **Ephemeral:** No (recommended for persistent services)
   - **Tags:** Add `tag:service` (or create a custom tag)
   - **Description:** "Video Download Service"
4. Copy the generated key and paste it into your `.env` file

### 3. Start the Services

Start both the application and Tailscale containers:

```bash
docker-compose up -d
```

This will start:
- `video-download` - The main application on port 8000
- `ts-video-download` - Tailscale sidecar container

### 4. Verify Tailscale Connection

Check that the Tailscale container connected successfully:

```bash
docker logs ts-video-download
```

You should see logs indicating successful connection to Tailscale network.

### 5. Find Your Tailscale Hostname

Check the Tailscale admin console at https://login.tailscale.com/admin/machines to find your device's hostname. It will be something like:

```
video-download.your-tailnet-name.ts.net
```

### 6. Access Your Service

You can now access your video download service securely via:

```
https://video-download.your-tailnet-name.ts.net
```

The Tailscale configuration automatically provides HTTPS with valid certificates!

## Configuration Files

### `ts-video-download/config/mcp.json`

This file configures how Tailscale serves your application:

```json
{
  "TCP": {
    "443": {
      "HTTPS": true
    }
  },
  "Web": {
    "${TS_CERT_DOMAIN}:443": {
      "Handlers": {
        "/": {
          "Proxy": "http://host.docker.internal:8000"
        }
      }
    }
  },
  "AllowFunnel": {
    "${TS_CERT_DOMAIN}:443": false
  }
}
```

- Proxies HTTPS requests on port 443 to your app on port 8000
- `AllowFunnel: false` means only Tailscale users can access (not public internet)

## Architecture

```
┌─────────────────────────────────────┐
│  Tailscale Network (Your Devices)  │
│                                     │
│  ┌──────────────────────────────┐  │
│  │   https://video-download     │  │
│  │   .your-tailnet.ts.net       │  │
│  └──────────────┬───────────────┘  │
│                 │                   │
└─────────────────┼───────────────────┘
                  │ HTTPS (443)
                  ▼
         ┌────────────────┐
         │  ts-video-     │
         │  download      │
         │  (Tailscale)   │
         └────────┬───────┘
                  │ HTTP (8000)
                  ▼
         ┌────────────────┐
         │  video-        │
         │  download      │
         │  (FastAPI)     │
         └────────────────┘
```

## Security Notes

- **Auth Key Security:** Keep your `.env` file secure and never commit it
- **Private Access:** By default, only devices on your Tailscale network can access the service
- **HTTPS:** Tailscale automatically provides valid TLS certificates
- **No Port Forwarding:** No need to expose ports on your router/firewall

## Troubleshooting

### Container won't start

Check if `/dev/net/tun` exists:
```bash
ls -l /dev/net/tun
```

If not, you may need to load the TUN module:
```bash
sudo modprobe tun
```

### Can't connect to service

1. Check both containers are running:
   ```bash
   docker-compose ps
   ```

2. Check Tailscale logs:
   ```bash
   docker logs ts-video-download
   ```

3. Verify the main app is accessible locally:
   ```bash
   curl http://localhost:8000
   ```

### Auth key expired

Generate a new auth key and update your `.env` file, then restart:
```bash
docker-compose restart ts-video-download
```

## Accessing from Other Devices

Any device connected to your Tailscale network can access the service:

1. **From your phone/tablet:**
   - Install Tailscale app
   - Sign in with your Tailscale account
   - Navigate to `https://video-download.your-tailnet-name.ts.net`

2. **From another computer:**
   - Install Tailscale
   - Sign in
   - Access the URL

## Advanced: Enabling Funnel (Public Access)

If you want to allow public internet access (anyone with the URL):

1. Edit `ts-video-download/config/mcp.json`
2. Change `"AllowFunnel": { "${TS_CERT_DOMAIN}:443": false }` to `true`
3. Restart containers: `docker-compose restart`

**Warning:** This makes your service publicly accessible. Ensure you have proper authentication/authorization in place!

## Stopping the Services

```bash
docker-compose down
```

To also remove volumes:
```bash
docker-compose down -v
```
