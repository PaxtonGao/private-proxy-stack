#!/bin/bash
# Subscription + sing-box config generator.
# Reads users.txt (name|uuid|short_id|sub_token) and produces:
#  - /etc/sing-box/config.json
#  - /opt/proxy-sub/sub/<token>/v2ray.txt   (base64 VLESS URI for Shadowrocket)
#  - /opt/proxy-sub/sub/<token>/singbox.json (full client config for Hiddify)

set -euo pipefail

USERS_FILE="/etc/sing-box/users.txt"
PRIV_KEY_FILE="/etc/sing-box/reality.key"
PUB_KEY_FILE="/etc/sing-box/reality.pub"
CLASH_SECRET_FILE="/etc/sing-box/clash_secret"
SB_CONFIG="/etc/sing-box/config.json"
SUB_DIR="/opt/proxy-sub/sub"
DUCKDNS_DOMAIN_FILE="/etc/duckdns/domain"
SERVER_IP="<VPS_IP>"               # << replace
SNI_PRIMARY="m.media-amazon.com"
SNI_BACKUP="s0.awsstatic.com"
SUB_PORT="8443"

usage() {
    cat <<EOF
Usage: gen.sh <command> [args]
Commands:
    generate              Regenerate sing-box config + all subscriptions
    add <name>            Add user, print subscription URL
    revoke <name>         Revoke user
    list                  List user names
    rotate                Rotate Reality keypair + every short_id + every token
    switch-sni            Swap primary/backup SNI
    show-sub <name>       Print user's subscription URL
EOF
}

current_sni() {
    if [ -f "$SB_CONFIG" ] && jq -e '.inbounds[0].tls.server_name' "$SB_CONFIG" >/dev/null 2>&1; then
        jq -r '.inbounds[0].tls.server_name' "$SB_CONFIG"
    else
        echo "$SNI_PRIMARY"
    fi
}

build_sb_config() {
    local sni priv clash_secret users_json sids_json
    sni=$(current_sni)
    priv=$(cat "$PRIV_KEY_FILE")
    clash_secret=$(cat "$CLASH_SECRET_FILE")

    users_json=$(awk -F'|' '{printf "{\"name\":\"%s\",\"uuid\":\"%s\",\"flow\":\"xtls-rprx-vision\"}\n", $1, $2}' "$USERS_FILE" | jq -s '.')
    sids_json=$(awk -F'|' '{print $3}' "$USERS_FILE" | jq -R . | jq -s '.')

    jq -n \
        --argjson users "$users_json" \
        --argjson sids "$sids_json" \
        --arg sni "$sni" \
        --arg priv "$priv" \
        --arg clash_secret "$clash_secret" \
        '{
            log: { level: "warn", timestamp: true },
            experimental: {
                clash_api: {
                    external_controller: "127.0.0.1:18090",
                    secret: $clash_secret
                }
            },
            inbounds: [{
                type: "vless",
                tag: "vless-in",
                listen: "::",
                listen_port: 443,
                users: $users,
                tls: {
                    enabled: true,
                    server_name: $sni,
                    reality: {
                        enabled: true,
                        handshake: { server: $sni, server_port: 443 },
                        private_key: $priv,
                        short_id: $sids
                    }
                }
            }],
            outbounds: [
                { type: "direct", tag: "direct" },
                { type: "block",  tag: "block"  }
            ]
        }' > "$SB_CONFIG"
    chown root:sing-box "$SB_CONFIG"
    chmod 640 "$SB_CONFIG"
}

build_user_sub() {
    local name=$1 uuid=$2 sid=$3 token=$4
    local pub sni
    pub=$(cat "$PUB_KEY_FILE")
    sni=$(current_sni)

    mkdir -p "$SUB_DIR/$token"

    local uri="vless://${uuid}@${SERVER_IP}:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=${sni}&fp=chrome&pbk=${pub}&sid=${sid}&type=tcp&headerType=none#${name}"
    echo -n "$uri" | base64 -w0 > "$SUB_DIR/$token/v2ray.txt"

    jq -n \
        --arg uuid "$uuid" --arg sni "$sni" --arg pub "$pub" --arg sid "$sid" --arg ip "$SERVER_IP" \
        '{
            log: { level: "warn" },
            dns: {
                servers: [
                    { tag: "remote", address: "https://1.1.1.1/dns-query", detour: "proxy" },
                    { tag: "local",  address: "https://223.5.5.5/dns-query", detour: "direct" }
                ],
                rules: [{ rule_set: "geosite-cn", server: "local" }]
            },
            inbounds: [{ type: "tun", tag: "tun-in", auto_route: true, strict_route: true, stack: "system" }],
            outbounds: [
                {
                    type: "vless", tag: "proxy",
                    server: $ip, server_port: 443,
                    uuid: $uuid, flow: "xtls-rprx-vision",
                    tls: {
                        enabled: true, server_name: $sni,
                        utls: { enabled: true, fingerprint: "chrome" },
                        reality: { enabled: true, public_key: $pub, short_id: $sid }
                    }
                },
                { type: "direct", tag: "direct" },
                { type: "block",  tag: "block"  }
            ],
            route: {
                rule_set: [
                    { type: "remote", tag: "geosite-cn",      format: "binary",
                      url: "https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-cn.srs",
                      download_detour: "proxy" },
                    { type: "remote", tag: "geoip-cn",        format: "binary",
                      url: "https://raw.githubusercontent.com/SagerNet/sing-geoip/rule-set/geoip-cn.srs",
                      download_detour: "proxy" },
                    { type: "remote", tag: "geosite-private", format: "binary",
                      url: "https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-private.srs",
                      download_detour: "proxy" }
                ],
                rules: [
                    { rule_set: "geosite-private", outbound: "direct" },
                    { rule_set: "geosite-cn",      outbound: "direct" },
                    { rule_set: "geoip-cn",        outbound: "direct" },
                    { ip_is_private: true,         outbound: "direct" }
                ],
                final: "proxy",
                auto_detect_interface: true
            }
        }' > "$SUB_DIR/$token/singbox.json"
}

print_url() {
    local token=$1
    local domain
    domain=$(cat "$DUCKDNS_DOMAIN_FILE" 2>/dev/null || echo "<DOMAIN_UNSET>")
    echo "https://${domain}.duckdns.org:${SUB_PORT}/sub/${token}"
}

cmd_generate() {
    build_sb_config
    rm -rf "$SUB_DIR"
    mkdir -p "$SUB_DIR"
    while IFS='|' read -r name uuid sid token; do
        [ -z "$name" ] && continue
        build_user_sub "$name" "$uuid" "$sid" "$token"
    done < "$USERS_FILE"
    if id caddy >/dev/null 2>&1; then
        chown -R caddy:caddy "$SUB_DIR" 2>/dev/null || true
        find "$SUB_DIR" -type d -exec chmod 755 {} \; 2>/dev/null || true
        find "$SUB_DIR" -type f -exec chmod 644 {} \; 2>/dev/null || true
    fi
    systemctl reload sing-box 2>/dev/null || systemctl restart sing-box 2>/dev/null || true
    echo "Generated. Active SNI: $(current_sni)"
}

cmd_add() {
    local name=$1
    if grep -q "^${name}|" "$USERS_FILE"; then
        echo "ERROR: user '$name' already exists"; exit 1
    fi
    local uuid sid token
    uuid=$(sing-box generate uuid)
    sid=$(openssl rand -hex 4)
    token=$(openssl rand -hex 16)
    echo "${name}|${uuid}|${sid}|${token}" >> "$USERS_FILE"
    cmd_generate >/dev/null
    print_url "$token"
}

cmd_revoke() {
    local name=$1
    if ! grep -q "^${name}|" "$USERS_FILE"; then
        echo "ERROR: user '$name' not found"; exit 1
    fi
    grep -v "^${name}|" "$USERS_FILE" > "${USERS_FILE}.tmp" && mv "${USERS_FILE}.tmp" "$USERS_FILE"
    chown root:sing-box "$USERS_FILE"
    chmod 640 "$USERS_FILE"
    cmd_generate >/dev/null
    echo "Revoked: $name"
}

cmd_list() {
    awk -F'|' '{print $1}' "$USERS_FILE"
}

cmd_show_sub() {
    local name=$1
    local token
    token=$(awk -F'|' -v n="$name" '$1==n {print $4}' "$USERS_FILE")
    if [ -z "$token" ]; then echo "ERROR: user '$name' not found"; exit 1; fi
    print_url "$token"
}

cmd_rotate() {
    OUT=$(sing-box generate reality-keypair)
    echo "$OUT" | grep PrivateKey | awk '{print $2}' > "$PRIV_KEY_FILE"
    echo "$OUT" | grep PublicKey  | awk '{print $2}' > "$PUB_KEY_FILE"
    chown root:sing-box "$PRIV_KEY_FILE" "$PUB_KEY_FILE"
    chmod 640 "$PRIV_KEY_FILE"
    chmod 644 "$PUB_KEY_FILE"
    awk -F'|' '{
        cmd1 = "openssl rand -hex 4";  cmd1 | getline sid;  close(cmd1)
        cmd2 = "openssl rand -hex 16"; cmd2 | getline tok;  close(cmd2)
        print $1"|"$2"|"sid"|"tok
    }' "$USERS_FILE" > "${USERS_FILE}.new" && mv "${USERS_FILE}.new" "$USERS_FILE"
    chown root:sing-box "$USERS_FILE"
    chmod 640 "$USERS_FILE"
    cmd_generate >/dev/null
    echo "Rotated. New subscription URLs:"
    while IFS='|' read -r name uuid sid token; do
        echo "  $name: $(print_url $token)"
    done < "$USERS_FILE"
}

cmd_switch_sni() {
    local current new
    current=$(current_sni)
    if [ "$current" = "$SNI_PRIMARY" ]; then new="$SNI_BACKUP"; else new="$SNI_PRIMARY"; fi
    jq --arg s "$new" '.inbounds[0].tls.server_name = $s | .inbounds[0].tls.reality.handshake.server = $s' "$SB_CONFIG" > "${SB_CONFIG}.new" && mv "${SB_CONFIG}.new" "$SB_CONFIG"
    chown root:sing-box "$SB_CONFIG"
    chmod 640 "$SB_CONFIG"
    cmd_generate >/dev/null
    echo "SNI switched: $current -> $new"
}

case "${1:-}" in
    generate)   cmd_generate ;;
    add)        cmd_add "$2" ;;
    revoke)     cmd_revoke "$2" ;;
    list)       cmd_list ;;
    rotate)     cmd_rotate ;;
    switch-sni) cmd_switch_sni ;;
    show-sub)   cmd_show_sub "$2" ;;
    *)          usage; exit 1 ;;
esac
