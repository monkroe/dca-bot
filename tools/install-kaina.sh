#!/data/data/com.termux/files/usr/bin/sh
# Installs `kaina` as a Termux command. Run once, from native Termux:
#   sh ~/workspace/projects/dca-bot/tools/install-kaina.sh
set -e
BIN="$PREFIX/bin/kaina"
SRC="$HOME/workspace/projects/dca-bot/tools/kaina.py"
[ -f "$SRC" ] || { echo "not found: $SRC"; exit 1; }
cat > "$BIN" <<'WRAP'
#!/data/data/com.termux/files/usr/bin/sh
exec python3 "$HOME/workspace/projects/dca-bot/tools/kaina.py" "$@"
WRAP
chmod +x "$BIN"
echo "installed: $BIN"
echo "try: kaina KAS"
