from flask import Flask, request, jsonify
import json, datetime, os, re, html as html_lib, traceback

app = Flask(__name__)
processed_emails = []

# Configuration from environment variables
AOAI_ENDPOINT = os.environ.get('AOAI_ENDPOINT', '')
AOAI_KEY = os.environ.get('AOAI_KEY', '')
AOAI_DEPLOYMENT = os.environ.get('AOAI_DEPLOYMENT', 'gpt-4o')
GRAPH_TOKEN = os.environ.get('GRAPH_TOKEN', '')
TEAMS_WEBHOOK_URL = os.environ.get('TEAMS_WEBHOOK_URL', '')
GRAPH_BASE = 'https://graph.microsoft.com/v1.0'


def strip_html(text):
    return html_lib.unescape(re.sub(r'<[^>]+>', '', text)).strip()


def summarize_with_aoai(subject, body_text):
    """Summarize email content using Azure OpenAI."""
    if not AOAI_ENDPOINT or not AOAI_KEY:
        return {'status': 'skipped', 'reason': 'AOAI not configured',
                'summary': f'[No AOAI] {subject}: {body_text[:200]}'}

    import requests
    url = f'{AOAI_ENDPOINT}/openai/deployments/{AOAI_DEPLOYMENT}/chat/completions?api-version=2024-02-15-preview'
    headers = {'api-key': AOAI_KEY, 'Content-Type': 'application/json'}
    payload = {
        'messages': [
            {'role': 'system', 'content': 'You are a concise email summarizer. Provide a 2-3 sentence summary of the email. Include key points, action items, and sentiment (positive/negative/neutral).'},
            {'role': 'user', 'content': f'Subject: {subject}\n\nBody:\n{body_text[:3000]}'}
        ],
        'max_tokens': 200,
        'temperature': 0.3
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            summary = resp.json()['choices'][0]['message']['content']
            return {'status': 'ok', 'summary': summary}
        return {'status': 'error', 'code': resp.status_code,
                'summary': f'[AOAI error {resp.status_code}] {subject}'}
    except Exception as e:
        return {'status': 'error', 'reason': str(e),
                'summary': f'[AOAI exception] {subject}'}


def save_to_onedrive(name, start_date, summary_text):
    """Save summary as a markdown file in OneDrive /EmailSummaries/."""
    if not GRAPH_TOKEN:
        return {'status': 'skipped', 'reason': 'GRAPH_TOKEN not configured'}

    import requests
    safe_name = re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '-')
    file_name = f'{start_date}-{safe_name}.md'
    headers = {'Authorization': f'Bearer {GRAPH_TOKEN}', 'Content-Type': 'text/plain'}

    # Ensure /EmailSummaries folder exists
    requests.post(f'{GRAPH_BASE}/me/drive/root/children',
        headers={'Authorization': f'Bearer {GRAPH_TOKEN}', 'Content-Type': 'application/json'},
        json={'name': 'EmailSummaries', 'folder': {},
              '@microsoft.graph.conflictBehavior': 'fail'})

    try:
        resp = requests.put(
            f'{GRAPH_BASE}/me/drive/root:/EmailSummaries/{file_name}:/content',
            headers=headers, data=summary_text.encode())
        if resp.status_code in (200, 201):
            return {'status': 'saved', 'path': f'/EmailSummaries/{file_name}'}
        return {'status': 'error', 'code': resp.status_code}
    except Exception as e:
        return {'status': 'error', 'reason': str(e)}


def post_to_teams(subject, from_addr, summary_text):
    """Post a notification to Teams via incoming webhook."""
    if not TEAMS_WEBHOOK_URL:
        return {'status': 'skipped', 'reason': 'TEAMS_WEBHOOK_URL not configured'}

    import requests
    card = {
        '@type': 'MessageCard',
        '@context': 'http://schema.org/extensions',
        'themeColor': '0076D7',
        'summary': f'New email summary: {subject}',
        'sections': [{
            'activityTitle': f'📧 {subject}',
            'activitySubtitle': f'From: {from_addr}',
            'text': summary_text,
            'markdown': True
        }]
    }

    try:
        resp = requests.post(TEAMS_WEBHOOK_URL, json=card, timeout=10)
        return {'status': 'posted' if resp.status_code == 200 else 'error',
                'code': resp.status_code}
    except Exception as e:
        return {'status': 'error', 'reason': str(e)}


@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle Office 365 email trigger — new email received."""
    payload = request.get_json(silent=True) or {}
    body = payload.get('body', payload)

    if isinstance(body, dict) and 'value' in body:
        items = body['value']
    elif isinstance(body, dict) and 'subject' in body:
        items = [body]
    else:
        items = [body] if body else []

    results = []
    for item in items:
        try:
            subject = item.get('subject', '(no subject)')
            body_preview = item.get('bodyPreview', '')
            body_content = item.get('body', body_preview)
            from_addr = item.get('from', 'unknown')
            received = item.get('receivedDateTime',
                datetime.datetime.now(datetime.timezone.utc).isoformat())

            body_text = strip_html(body_content) if isinstance(body_content, str) else body_preview
            date_str = received[:10]

            # Step 1: Summarize with Azure OpenAI
            aoai_result = summarize_with_aoai(subject, body_text)
            summary_text = aoai_result.get('summary', '')

            # Step 2: Build markdown summary
            md_content = f'# Email Summary\n\n'
            md_content += f'**Subject:** {subject}\n'
            md_content += f'**From:** {from_addr}\n'
            md_content += f'**Received:** {received}\n\n'
            md_content += f'## Summary\n\n{summary_text}\n\n'
            md_content += f'## Original Preview\n\n{body_preview[:500]}\n'

            # Step 3: Save to OneDrive
            onedrive_result = save_to_onedrive(subject, date_str, md_content)

            # Step 4: Post to Teams
            teams_result = post_to_teams(subject, from_addr, summary_text)

            entry = {
                'id': len(processed_emails),
                'subject': subject,
                'from': from_addr,
                'received': received,
                'body_preview': body_preview[:200],
                'summary': summary_text,
                'actions': {
                    'aoai': aoai_result.get('status', 'unknown'),
                    'onedrive': onedrive_result.get('status', 'unknown'),
                    'teams': teams_result.get('status', 'unknown'),
                },
                'details': {
                    'aoai': aoai_result,
                    'onedrive': onedrive_result,
                    'teams': teams_result,
                },
                'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat()
            }
            processed_emails.append(entry)
            results.append(entry)

        except Exception as e:
            results.append({'error': str(e), 'traceback': traceback.format_exc()})

    return jsonify(status='processed', count=len(results), results=results)


@app.route('/')
def dashboard():
    """Email summarizer dashboard."""
    html = '<html><head><style>'
    html += 'body{font-family:sans-serif;max-width:900px;margin:0 auto;padding:20px}'
    html += '.card{border:1px solid #ddd;border-radius:8px;padding:16px;margin:12px 0;border-left:5px solid #0078d4}'
    html += '.stats{display:flex;gap:20px;margin:16px 0;flex-wrap:wrap} .stat{padding:12px 24px;border-radius:8px;text-align:center;min-width:100px}'
    html += '.stat-total{background:#e7f3ff;color:#0078d4} .stat-aoai{background:#d4edda;color:#155724}'
    html += '.stat-drive{background:#fff3cd;color:#856404} .stat-teams{background:#e8daef;color:#6c3483}'
    html += '.summary{background:#f8f9fa;padding:12px;border-radius:6px;margin:8px 0;font-style:italic}'
    html += '.actions{display:flex;gap:8px;margin:8px 0} .action{padding:4px 10px;border-radius:12px;font-size:12px}'
    html += '.ok{background:#d4edda;color:#155724} .skip{background:#fff3cd;color:#856404} .err{background:#f8d7da;color:#721c24}'
    html += '</style></head><body>'
    html += '<h1>&#128231; Email Summarizer Dashboard</h1>'
    html += '<p>AI-powered email summaries with OneDrive storage and Teams notifications</p>'

    total = len(processed_emails)
    aoai_ok = sum(1 for e in processed_emails if e['actions']['aoai'] == 'ok')
    drive_ok = sum(1 for e in processed_emails if e['actions']['onedrive'] == 'saved')
    teams_ok = sum(1 for e in processed_emails if e['actions']['teams'] == 'posted')

    html += '<div class="stats">'
    html += f'<div class="stat stat-total"><b>{total}</b><br>Emails</div>'
    html += f'<div class="stat stat-aoai"><b>{aoai_ok}</b><br>Summarized</div>'
    html += f'<div class="stat stat-drive"><b>{drive_ok}</b><br>Saved</div>'
    html += f'<div class="stat stat-teams"><b>{teams_ok}</b><br>Notified</div>'
    html += '</div>'

    # Config status
    html += '<details><summary><b>Configuration Status</b></summary><ul>'
    html += f'<li>Azure OpenAI: {"✅ Configured" if AOAI_ENDPOINT else "⚠️ Not set (AOAI_ENDPOINT)"}</li>'
    html += f'<li>OneDrive: {"✅ Configured" if GRAPH_TOKEN else "⚠️ Not set (GRAPH_TOKEN)"}</li>'
    html += f'<li>Teams: {"✅ Configured" if TEAMS_WEBHOOK_URL else "⚠️ Not set (TEAMS_WEBHOOK_URL)"}</li>'
    html += '</ul></details><hr>'

    if not processed_emails:
        html += '<p><i>No emails processed yet. Send an email to your monitored inbox to trigger the flow!</i></p>'
    else:
        for e in reversed(processed_emails[-30:]):
            action_class = lambda s: 'ok' if s in ('ok', 'saved', 'posted') else ('skip' if s == 'skipped' else 'err')
            html += '<div class="card">'
            html += f'<h3>&#128233; {e["subject"]}</h3>'
            html += f'<p><b>From:</b> {e["from"]} &middot; <b>Received:</b> {e["received"][:19]}</p>'
            html += '<div class="actions">'
            html += f'<span class="action {action_class(e["actions"]["aoai"])}">🤖 AOAI: {e["actions"]["aoai"]}</span>'
            html += f'<span class="action {action_class(e["actions"]["onedrive"])}">📁 OneDrive: {e["actions"]["onedrive"]}</span>'
            html += f'<span class="action {action_class(e["actions"]["teams"])}">💬 Teams: {e["actions"]["teams"]}</span>'
            html += '</div>'
            if e.get('summary'):
                html += f'<div class="summary">{e["summary"][:300]}</div>'
            html += '</div>'

    html += '</body></html>'
    return html


@app.route('/api/emails')
def api_emails():
    """JSON API for processed emails."""
    return jsonify(
        emails=processed_emails,
        total=len(processed_emails),
        config={
            'aoai': bool(AOAI_ENDPOINT),
            'onedrive': bool(GRAPH_TOKEN),
            'teams': bool(TEAMS_WEBHOOK_URL),
        })


if __name__ == '__main__':
    print('Email Summarizer starting on :5000')
    print(f'  AOAI:     {"configured" if AOAI_ENDPOINT else "not set"}')
    print(f'  OneDrive: {"configured" if GRAPH_TOKEN else "not set"}')
    print(f'  Teams:    {"configured" if TEAMS_WEBHOOK_URL else "not set"}')
    app.run(host='0.0.0.0', port=5000)
