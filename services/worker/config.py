"""Worker settings that are neither secrets nor per-deployment — see runner.py."""

# A hung connect must not outlive the systemd unit's own start timeout
# (ops/hotel-worker.service, TimeoutStartSec), which is the outer bound on a
# whole pass; this is the inner bound on the one network call that can block
# before any job has run.
DATABASE_CONNECT_TIMEOUT_SECONDS = 10
