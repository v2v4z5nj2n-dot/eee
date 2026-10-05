#!/bin/bash
cd "$(dirname "$0")"
[ -d venv ] && source venv/bin/activate
caffeinate -i python3 bot.py run
