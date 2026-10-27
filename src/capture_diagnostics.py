"""Recent, per-host TLS incidents. Compatible with the frozen proxy runtime."""
import time

WINDOW = 120
FRESH = 60
MIN_FAILURES = 3
MIN_DURATION = 10


def tls_host(data):
    conn = data.conn
    server = getattr(getattr(data, 'context', None), 'server', None)
    address = getattr(server, 'address', None)
    return str(getattr(conn, 'sni', None) or getattr(server, 'sni', None)
               or (address[0] if address else 'Unknown destination')).lower()


def record_failure(health, host, reason, now=None):
    now = time.time() if now is None else now
    hosts = health.setdefault('tls_hosts', {})
    incident = hosts.setdefault(host, {})
    recent = [t for t in incident.get('failures', []) if now - WINDOW <= t <= now]
    if not recent:
        incident['incident_start'] = now
    incident.update(failures=(recent + [now])[-100:], last_error=str(reason or 'TLS handshake failed'), last_failure=now)


def record_success(health, host, now=None):
    incident = health.get('tls_hosts', {}).get(host)
    if incident is None:
        return False
    incident.update(failures=[], last_success=time.time() if now is None else now)
    return True


def current_issues(health, running, now=None):
    """Old one-line warnings are not evidence of a current connection problem."""
    if not running:
        return []
    now = time.time() if now is None else now
    issues = []
    for host, incident in sorted(health.get('tls_hosts', {}).items()):
        failures = [t for t in incident.get('failures', []) if now - WINDOW <= t <= now]
        if (len(failures) < MIN_FAILURES or now - max(failures) > FRESH
                or max(failures) - min(failures) < MIN_DURATION
                or incident.get('last_success', 0) >= max(failures)):
            continue
        issues.append({'id': f"tls:{health.get('session', '')}:{host}:{incident['incident_start']}",
                'title': 'Repeated secure connection failures',
                'message': f'Connections to {host} have repeatedly failed and have not recovered. '
                           'Some traffic to this destination may be missing.\n\n'
                           f"Latest error: {incident.get('last_error', 'TLS handshake failed')}\n\n"
                           'Check certificate trust for the affected app. A TLS error can also have other causes. '
                           'Details are saved in capture-health.json.'})
    return issues
