# Offline guide: fix the Pi-hole network, then install One Inbox

Download this file and `one-inbox.zip` **before** you switch to the Pi-hole network. Everything below works without internet.

On the Mac, open Terminal. Replace `PI` with your Pi's address (for example `192.168.1.2`) and `pi` with your Pi username.

---

## Part A. Why everything stops on the Pi-hole network

Work down the list. Stop at the first step that fails: that is the problem.

### A1. Can you reach the Pi at all?

```bash
ping -c 3 PI
ssh pi@PI
```

- **Ping fails:** your Mac is not on the same network as the Pi, or the Pi is off. Check the Wi-Fi name, and that the Pi's network light blinks. Skip to A6.
- **Ping works:** go on.

### A2. Is it names or everything?

On the Mac:

```bash
ping -c 3 1.1.1.1        # an address, no DNS needed
ping -c 3 google.com     # needs DNS
```

| 1.1.1.1 | google.com | Meaning | Go to |
| --- | --- | --- | --- |
| works | fails | **DNS is broken** (most likely) | A3 |
| fails | fails | **Routing is broken**: no way out to the internet | A6 |
| works | works | Internet is fine; the problem is elsewhere, maybe a blocked app | A7 |

**Get back online right now, while you fix it:** System Settings → Network → Wi-Fi → Details → DNS → `+` → `1.1.1.1`. Remove it again once Pi-hole works. On iPhone: Settings → Wi-Fi → (i) → Configure DNS → Manual → `1.1.1.1`.

### A3. Is Pi-hole running?

On the Pi (`ssh pi@PI`):

```bash
pihole status
sudo systemctl status pihole-FTL --no-pager
```

If it's not running or says failed:

```bash
sudo systemctl restart pihole-FTL
pihole status
```

Still failing? See the last lines of its log:

```bash
sudo tail -n 40 /var/log/pihole/FTL.log
```

A message about **port 53 already in use** means another DNS service has taken the port:

```bash
sudo ss -lunp | grep ':53 '
```

If you see `systemd-resolved` or `dnsmasq` (not `pihole-FTL`), stop it and restart Pi-hole:

```bash
sudo systemctl disable --now systemd-resolved   # or: dnsmasq
sudo systemctl restart pihole-FTL
```

### A4. Can Pi-hole answer?

On the Pi:

```bash
dig @127.0.0.1 google.com +short     # Pi-hole itself
dig @1.1.1.1 google.com +short       # straight to the internet
```

(No `dig`? Use `nslookup google.com 127.0.0.1`.)

- **Both fail:** the Pi itself has no internet. Go to A6, but run the checks on the Pi.
- **Only the first fails:** Pi-hole's upstream is wrong or blocked. Go to A5.
- **Both work, but the Mac still can't resolve:** Pi-hole is refusing your Mac. Go to A5, "Interface".

### A5. Fix Pi-hole's settings

**Clock first.** A Pi has no battery clock. If the date is wrong, DNSSEC checks fail and every lookup fails with them.

```bash
date
timedatectl
```

If the date is wrong, set it by hand (use the real time), then turn DNSSEC off until things work:

```bash
sudo date -s "2026-10-26 08:00:00"
```

Then pick the commands for your Pi-hole version (`pihole -v` tells you).

**Pi-hole v6:**

```bash
sudo pihole-FTL --config dns.upstreams            # shows the current upstreams
sudo pihole-FTL --config dns.upstreams '["1.1.1.1","9.9.9.9"]'
sudo pihole-FTL --config dns.dnssec false
sudo pihole-FTL --config dns.listeningMode ALL    # "Interface" fix: answer every device on the network
sudo systemctl restart pihole-FTL
```

If you use **unbound** (upstream shows `127.0.0.1#5335`), check that it is running first:

```bash
sudo systemctl status unbound --no-pager
dig @127.0.0.1 -p 5335 google.com +short
```

If unbound fails, either `sudo systemctl restart unbound`, or switch to `1.1.1.1` as shown above for now.

**Pi-hole v5:**

```bash
grep -E 'PIHOLE_DNS|DNSSEC|DNSMASQ_LISTENING' /etc/pihole/setupVars.conf
sudo sed -i 's/^PIHOLE_DNS_1=.*/PIHOLE_DNS_1=1.1.1.1/; s/^DNSSEC=.*/DNSSEC=false/; s/^DNSMASQ_LISTENING=.*/DNSMASQ_LISTENING=all/' /etc/pihole/setupVars.conf
pihole -r     # choose "Repair"
```

Test again from the Mac: `ping google.com`.

**Something you need is blocked?** Pause blocking for 5 minutes to test: `pihole disable 5m`. If that fixes it, a blocklist is the cause: run `pihole -t` and open the app to see which domain gets blocked, then `pihole allow that.domain.com` (v5: `pihole -w that.domain.com`).

### A6. No route out (1.1.1.1 fails)

On the Mac:

```bash
route -n get default | grep gateway
```

The gateway should be your **router** (often `192.168.1.1` or `192.168.0.1`), **not** the Pi.

- **Gateway is the Pi's address:** Pi-hole's DHCP is handing out the wrong router. Fix the router address in Pi-hole's DHCP settings:
  - v6: `sudo pihole-FTL --config dhcp.router 192.168.1.1` (your real router), then `sudo systemctl restart pihole-FTL`
  - v5: web page → Settings → DHCP → "Router (gateway) IP address"
  - Or turn Pi-hole's DHCP off and let the router do DHCP again: v6 `sudo pihole-FTL --config dhcp.active false`.
  - Then on the Mac, turn Wi-Fi off and on to get a fresh address.
- **Two DHCP servers** (the router's and Pi-hole's both on) give random results. Keep only one. Easiest: router DHCP on, Pi-hole DHCP off, and set the router's DNS to the Pi.
- **Tailscale on the Pi** acting as an exit node or subnet router can also capture traffic. Test with `sudo tailscale down`. If that fixes it, bring it back with `sudo tailscale up` without the `--advertise-exit-node` or `--advertise-routes` options.

On the Pi itself:

```bash
ip route | head -3            # "default via <router>" must be there
ping -c 3 <router address>
```

No `default via` line means the Pi has a static address without a gateway. Check `/etc/dhcpcd.conf` (`static routers=`) or NetworkManager: `nmcli con show` then `sudo nmcli con mod "<name>" ipv4.gateway 192.168.1.1 && sudo nmcli con up "<name>"`.

### A7. Still stuck

Collect the details for later, when you are back online:

```bash
pihole -d     # answer "no" to uploading; it saves a report under /var/log/pihole/
```

Turn Pi-hole off until then with `pihole disable` (back on with `pihole enable`), and keep `1.1.1.1` set on your Mac and phone.

---

## Part B. Install One Inbox without internet

Nothing to download on the Pi: it only needs Python 3, which Raspberry Pi OS already has.

### B1. Copy the files to the Pi

On the Mac, from the folder where you saved `one-inbox.zip` (usually Downloads):

```bash
cd ~/Downloads
scp one-inbox.zip pi@PI:~
ssh pi@PI
```

Now on the Pi:

```bash
python3 -m zipfile -e one-inbox.zip one-inbox    # works without the unzip tool
cd one-inbox/one-inbox-*                          # the folder inside
python3 --version                                 # needs 3.9 or newer
```

### B2. Check it, then install

```bash
python3 -m unittest discover -s tests -t .       # should end with "OK"
sudo bash deploy/install-pi.sh
```

The script needs no internet. It ends with `One Inbox is running.` Then:

```bash
sudo cat /etc/onebox.env      # your three keys: write them down or keep this terminal open
hostname -I                   # the Pi's address
```

### B3. Try it from your Mac or iPhone

Open `http://PI:8787` in the browser and enter `ONEBOX_UI_KEY`. You'll see "waiting for first Mac sync". That's expected.

### B4. Give it a name (optional, works offline)

- Pi-hole v6: web page → Settings → Local DNS Records → `inbox.home` → the Pi's address.
- Or on the Pi: `sudo pihole-FTL --config dns.hosts '["192.168.1.2 inbox.home"]'` (your Pi's address), then `sudo systemctl restart pihole-FTL`. This replaces any local records you already have, so use the web page if you have others.

Now `http://inbox.home:8787` works on any device that uses Pi-hole.

### B5. The Mac agent

Copy the same zip to your Mac's home folder and unzip it, then follow **section 4 of `docs/setup.md`**, which is inside the zip. Use `http://PI:8787` as the `server` for now. Change it to the Tailscale address later, once you have internet again.

### Later, with internet

- Tailscale (section 3 of `docs/setup.md`) needs internet to install. Do it once the network works.
- Updates: download a new zip and repeat B1 and B2. Your keys and data are kept.
