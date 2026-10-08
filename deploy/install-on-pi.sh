#!/bin/zsh
# One-time install on the machine that publishes (run from a Mac): PI=user@host deploy/install-on-pi.sh
# Clones the repo with its own deploy key and installs the systemd units for the remote user.
# Needs: the deploy key already added to github.com/GumDropSoftware/overhead-satellites with write access.
set -euo pipefail
PI=${PI:?set PI=user@host}
ssh "$PI" 'set -e
  grep -q "Host github-overhead-satellites" ~/.ssh/config || cat >> ~/.ssh/config <<CFG

Host github-overhead-satellites
  HostName github.com
  User git
  IdentityFile ~/.ssh/overhead_satellites_deploy
  IdentitiesOnly yes
CFG
  [ -d ~/projects/overhead-satellites ] || git clone -q git@github-overhead-satellites:GumDropSoftware/overhead-satellites.git ~/projects/overhead-satellites
  git -C ~/projects/overhead-satellites config user.name "Gumdrop Software"
  git -C ~/projects/overhead-satellites config user.email "255791754+GumDropSoftware@users.noreply.github.com"
  sed "s|@USER@|$USER|g; s|@HOME@|$HOME|g" ~/projects/overhead-satellites/deploy/overhead-satellites.service | sudo tee /etc/systemd/system/overhead-satellites.service >/dev/null
  sudo cp ~/projects/overhead-satellites/deploy/overhead-satellites.timer /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now overhead-satellites.timer
  systemctl list-timers --no-pager | grep overhead-satellites'
