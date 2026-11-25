Deploy the PartsBridge PK app to my Oracle Cloud server and serve it publicly.

Repo:   https://github.com/AhmadMujtaba200210/cloud-drive
Branch: claude/global-car-parts-marketplace-1deni7
Stack:  FastAPI app + SearXNG + Firecrawl Local (Rust) + nginx, all free/self-hosted. The installer does everything.

Steps on the server (SSH in first):
1. git clone -b claude/global-car-parts-marketplace-1deni7 https://github.com/AhmadMujtaba200210/cloud-drive.git
   cd cloud-drive
2. Run the installer (pick one):
     sudo deploy/install.sh                                              # http://<public-ip>/
     sudo DOMAIN=<my-domain> EMAIL=<my-email> deploy/install.sh          # HTTPS via Let's Encrypt (DNS A record must point at the VM first)
   First run builds Firecrawl Local with cargo: ~10-20 min (longer on ARM Ampere). Safe to re-run.
3. Put the LLM keys in /etc/partsbridge/env (I will give them to you directly; never commit them):
     LAUNCHSHIELD_GROQ_API_KEY=...
     LAUNCHSHIELD_OPENROUTER_API_KEY=...
     LAUNCHSHIELD_GEMINI_API_KEY=...
   then: sudo systemctl restart partsbridge-app
4. OCI console: VCN -> Subnet -> Security List -> add Ingress rules TCP 80 and TCP 443 from 0.0.0.0/0.
   (install.sh already opens the OS firewall, inserting before Oracle's default REJECT rule.)
5. Verify:
     systemctl is-active partsbridge-searxng partsbridge-firecrawl partsbridge-app nginx   # all "active"
     curl -s http://127.0.0.1/healthz                                                      # {"status":"ok"}
     curl -s -X POST http://127.0.0.1/api/search -H 'Content-Type: application/json' \
       -d '{"make":"Honda","model":"Civic","year":2018,"part_name":"brake pads","customer_city":"Lahore"}' | head -c 400
     Then open http://<public-ip>/ from outside the VM.
   A full search takes ~1-2.5 minutes (nginx allows 300 s).

Notes:
- Only nginx is public; app (8000), SearXNG (8888), Firecrawl (3002) bind to 127.0.0.1.
- Rate limits: nginx 6 searches/min per IP (burst 3) + app 30/hour per IP (RATE_LIMIT_PER_HOUR).
  Optional private mode: set ACCESS_TOKEN=... in /etc/partsbridge/env, open https://host/?token=VALUE.
- Logs: journalctl -u partsbridge-app -f   (also -searxng, -firecrawl).  Update later: sudo deploy/update.sh
- Tested end to end on Ubuntu 24.04 (without real systemd). Not yet run on real systemd or on ARM, so report
  any failing service with its journalctl output.
- If the repo is private, the clone needs a GitHub token or deploy key.

When done, tell me the public URL and the output of step 5.
