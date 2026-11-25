#!/bin/zsh

cd -- "${0:A:h}" || exit 1

website_url="http://127.0.0.1:8080"

# Reuse the console when it is already running.
if /usr/bin/curl --silent --fail --max-time 2 "$website_url/api/latest" >/dev/null; then
    /usr/bin/open "$website_url"
    exit 0
fi

if [[ ! -x .venv/bin/python ]]; then
    print 'The project Python environment is missing.'
    print 'Create it with: python3 -m venv .venv'
    print 'Then install: .venv/bin/python -m pip install pyserial'
    read -r '?Press Return to close.'
    exit 1
fi

print 'Starting SRT Telemetry. Keep this window open while using the website.'
print 'Press Control-C to stop the website.'
.venv/bin/python app.py --web-only --dashboard-host 127.0.0.1 --dashboard-port 8080 --data-dir /tmp/srt-bench-data &
website_pid=$!
trap 'kill "$website_pid" 2>/dev/null; wait "$website_pid" 2>/dev/null' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

for attempt in {1..50}; do
    if ! kill -0 "$website_pid" 2>/dev/null; then
        print 'The website could not start. See the error above.'
        read -r '?Press Return to close.'
        exit 1
    fi
    if /usr/bin/curl --silent --fail --max-time 1 "$website_url/api/latest" >/dev/null; then
        /usr/bin/open "$website_url"
        wait "$website_pid"
        exit $?
    fi
    sleep 0.2
done

print "The website is taking longer than expected. Open $website_url when ready."
wait "$website_pid"
