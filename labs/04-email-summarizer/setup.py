#!/usr/bin/env python3
"""Interactive setup for the Email Summarizer lab.

Walks you through creating a connector gateway, Office 365 connection,
sandbox, and email trigger — step by step with prompts.

Usage:
    python setup.py             # Interactive setup
    python setup.py --cleanup   # Delete all resources
"""

import os, sys, json, subprocess, time

_SHELL = sys.platform == 'win32'


def az_account():
    result = subprocess.run(
        ['az', 'account', 'show', '-o', 'json'],
        capture_output=True, text=True, check=True, shell=_SHELL)
    return json.loads(result.stdout)


def prompt(question, default=None):
    suffix = f' [{default}]' if default else ''
    answer = input(f'{question}{suffix}: ').strip()
    return answer or default


def prompt_choice(question, choices):
    print(f'\n{question}')
    for i, c in enumerate(choices, 1):
        print(f'  {i}. {c}')
    while True:
        answer = input(f'Select (1-{len(choices)}): ').strip()
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        print(f'  Please enter a number between 1 and {len(choices)}')


def main():
    from azure.connectorgateway import ConnectorGatewayClient, TriggerClient
    from azure.sandbox import SandboxClient
    from azure.mgmt.sandbox import SandboxGroupManagementClient

    print('=' * 60)
    print('  Email Summarizer — Interactive Setup')
    print('=' * 60)

    # --- Step 0: Azure account ---
    account = az_account()
    subscription_id = account['id']
    print(f'\nUser:         {account["user"]["name"]}')
    print(f'Subscription: {account["name"]} ({subscription_id})')

    resource_group = prompt('\nResource group name', 'lab-04-email-summarizer')
    location = prompt('Location', 'eastus2')
    gateway_name = prompt('Gateway name', 'email-summarizer-gw')
    connection_name = 'o365-conn'
    sandbox_group_name = prompt('Sandbox group name', 'email-summarizer-sg')
    trigger_config_name = 'email-trigger'

    conn_client = ConnectorGatewayClient(subscription_id=subscription_id, resource_group=resource_group)
    trigger_client = TriggerClient(subscription_id=subscription_id, resource_group=resource_group)
    sbx_client = SandboxClient(subscription_id=subscription_id, resource_group=resource_group)
    mgmt = SandboxGroupManagementClient(subscription_id=subscription_id, resource_group=resource_group)

    # --- Step 1: Resource group ---
    print(f'\n--- Step 1: Create resource group ---')
    subprocess.run(
        ['az', 'group', 'create', '--name', resource_group, '--location', location, '-o', 'none'],
        check=True, shell=_SHELL)
    print(f'✅ Resource group: {resource_group} ({location})')

    # --- Step 2: Connector gateway ---
    print(f'\n--- Step 2: Create connector gateway ---')
    try:
        gw = conn_client.get_gateway(gateway_name)
        print(f'Gateway: {gateway_name} (already exists)')
    except Exception:
        gw = conn_client.create_gateway(gateway_name, location='brazilsouth',
            identity={'type': 'SystemAssigned'})
        print(f'✅ Gateway: {gateway_name} (created)')

    gw_principal_id = gw['identity']['principalId']
    gw_tenant_id = gw['identity']['tenantId']
    gw_location = gw.get('location', 'brazilsouth')
    print(f'Principal ID: {gw_principal_id}')

    # --- Step 3: Office 365 connection + OAuth consent ---
    print(f'\n--- Step 3: Create Office 365 connection ---')
    try:
        conn_client.create_connection(gateway_name, connection_name, connector_name='office365')
        print(f'Connection: {connection_name} (created)')
    except Exception as e:
        if '409' in str(e) or 'Conflict' in str(e):
            print(f'Connection: {connection_name} (already exists)')
        else:
            raise

    conn = conn_client.get_connection(gateway_name, connection_name)
    status = conn.get('properties', {}).get('statuses', [{}])[0].get('status', 'Unknown')

    if status != 'Connected':
        link = conn_client.generate_consent_link(gateway_name, connection_name)
        print(f'\n⚠️  Please authenticate by clicking this link:')
        print(f'    {link}')
        print(f'\n    (The link expires quickly — click it now!)')
        input('\n    Press Enter after you have completed authentication...')

        conn = conn_client.get_connection(gateway_name, connection_name)
        status = conn.get('properties', {}).get('statuses', [{}])[0].get('status', 'Unknown')
        if status == 'Connected':
            print(f'✅ Connection authenticated!')
        else:
            print(f'⚠️  Connection status: {status} — may need to re-authenticate')
    else:
        print(f'✅ Connection already authenticated!')

    # --- Step 4: Discover trigger operations ---
    print(f'\n--- Step 4: Discover email trigger operations ---')
    try:
        ops = trigger_client.list_trigger_operations(gateway_name, 'office365')
        email_ops = [op for op in ops if 'email' in op.get('summary', '').lower()
                     or 'mail' in op.get('operationId', '').lower()]
        if email_ops:
            print(f'Found {len(email_ops)} email trigger operations:')
            for op in email_ops:
                print(f'  • {op["operationId"]}: {op.get("summary", "")}')
        operation_name = 'OnNewEmailV3'
        print(f'\nUsing: {operation_name}')
    except Exception as e:
        print(f'Could not discover operations: {e}')
        operation_name = 'OnNewEmailV3'
        print(f'Using default: {operation_name}')

    # --- Step 5: Trigger parameters ---
    print(f'\n--- Step 5: Configure trigger parameters ---')
    folder_path = prompt('Email folder to monitor', 'Inbox')
    subject_filter = prompt('Subject filter (leave empty for all emails)', '')

    parameters = [{'name': 'folderPath', 'value': folder_path}]
    if subject_filter:
        parameters.append({'name': 'subjectFilter', 'value': subject_filter})
    print(f'Folder: {folder_path}')
    if subject_filter:
        print(f'Subject filter: {subject_filter}')

    # --- Step 6: Azure OpenAI configuration ---
    print(f'\n--- Step 6: Azure OpenAI configuration ---')
    print('The app uses Azure OpenAI to summarize emails.')
    print('You can skip this — the app will still log emails without summaries.\n')

    aoai_endpoint = prompt('Azure OpenAI endpoint (or press Enter to skip)', '')
    aoai_key = ''
    aoai_deployment = 'gpt-4o'
    if aoai_endpoint:
        aoai_key = prompt('Azure OpenAI API key', '')
        aoai_deployment = prompt('Deployment name', 'gpt-4o')
        print(f'✅ AOAI configured: {aoai_endpoint} / {aoai_deployment}')
    else:
        print('⏭️  Skipping Azure OpenAI — emails will be logged without AI summaries')

    # --- Step 7: Teams webhook (optional) ---
    print(f'\n--- Step 7: Teams webhook (optional) ---')
    teams_url = prompt('Teams incoming webhook URL (or press Enter to skip)', '')
    if teams_url:
        print(f'✅ Teams webhook configured')
    else:
        print(f'⏭️  Skipping Teams notifications')

    # --- Step 8: Create sandbox ---
    print(f'\n--- Step 8: Create sandbox ---')
    group = mgmt.create_group(sandbox_group_name, location=location)
    print(f'Sandbox group: {group["name"]}')

    sandboxes = sbx_client.list_sandboxes(sandbox_group_name)
    if sandboxes:
        sandbox_id = sandboxes[0]['id']
        print(f'Sandbox: {sandbox_id} (existing)')
    else:
        sbx = sbx_client.create_sandbox(sandbox_group_name, disk='ubuntu')
        sandbox_id = sbx['id']
        print(f'Sandbox: {sandbox_id} (created)')

    print('Waiting for sandbox to be Running...')
    resumed = False
    for i in range(36):
        info = sbx_client.get_sandbox(sandbox_id, sandbox_group_name)
        state = info.get('state', 'Unknown')
        print(f'  [{i+1}] state={state}')
        if state == 'Running':
            break
        if state == 'Idle' and not resumed:
            sbx_client.resume_sandbox(sandbox_id, sandbox_group_name)
            resumed = True
        time.sleep(5)
    else:
        print('ERROR: Sandbox did not reach Running state in 3 minutes')
        sys.exit(1)
    print(f'✅ Sandbox is running!')

    # --- Step 9: Install dependencies + deploy app ---
    print(f'\n--- Step 9: Deploy Flask app ---')
    sbx_client.exec(sandbox_id, sandbox_group_name,
        'dpkg -s python3-flask >/dev/null 2>&1 || '
        '(apt-get update -qq && apt-get install -y -qq python3-flask python3-requests 2>&1 | tail -1)')

    app_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app')
    with open(os.path.join(app_dir, 'server.py'), 'r') as f:
        app_code = f.read()

    sbx_client.exec(sandbox_id, sandbox_group_name, 'pkill -f server.py 2>/dev/null; sleep 1; true')
    sbx_client.write_file(sandbox_id, sandbox_group_name, '/app/server.py', app_code)
    print('Uploaded /app/server.py')

    # Build env vars for the app
    env_parts = []
    if aoai_endpoint:
        env_parts.append(f'AOAI_ENDPOINT="{aoai_endpoint}"')
    if aoai_key:
        env_parts.append(f'AOAI_KEY="{aoai_key}"')
    if aoai_deployment:
        env_parts.append(f'AOAI_DEPLOYMENT="{aoai_deployment}"')
    if teams_url:
        env_parts.append(f'TEAMS_WEBHOOK_URL="{teams_url}"')
    env_str = ' '.join(env_parts)

    sbx_client.exec(sandbox_id, sandbox_group_name,
        f'nohup {env_str} python3 /app/server.py > /tmp/server.log 2>&1 &')
    time.sleep(2)

    r = sbx_client.exec(sandbox_id, sandbox_group_name,
        'curl -s http://localhost:5000/ | head -c 100')
    if 'Summarizer' in r.get('stdout', ''):
        print('✅ Flask server is running!')
    else:
        print('⚠️  Server may still be starting...')

    try:
        sbx_client.add_port(sandbox_id, sandbox_group_name, 5000, anonymous=True)
    except Exception:
        pass

    dashboard_url = f'https://{sandbox_id}--5000.proxy.azuredevcompute.io'
    print(f'Dashboard: {dashboard_url}')

    # --- Step 10: Create trigger config ---
    print(f'\n--- Step 10: Create email trigger ---')
    try:
        trigger_client.delete_trigger(gateway_name, trigger_config_name)
        print(f'Deleted existing trigger: {trigger_config_name}')
    except Exception:
        pass

    trigger = trigger_client.create_trigger(
        gateway_name, trigger_config_name,
        connector_name='office365',
        connection_name=connection_name,
        operation_name=operation_name,
        sandbox_id=sandbox_id,
        sandbox_group=sandbox_group_name,
        port=5000,
        port_path='/webhook',
        http_method='POST',
        description='Email Summarizer — summarizes new emails with Azure OpenAI',
        parameters=parameters)
    print(f'✅ Trigger created: {trigger_config_name}')
    print(f'State: {trigger["properties"]["state"]}')

    # --- Step 11: Access policy + port auth ---
    print(f'\n--- Step 11: Access policy + port auth ---')
    try:
        conn_client.create_access_policy(gateway_name, connection_name,
            principal_id=gw_principal_id,
            tenant_id=gw_tenant_id,
            location=gw_location)
        print(f'✅ Access policy granted')
    except Exception as e:
        if '409' in str(e) or 'Conflict' in str(e):
            print(f'✅ Access policy already exists')
        else:
            raise

    # --- Step 12: Verify ---
    print(f'\n--- Step 12: Verify trigger ---')
    tc = trigger_client.get_trigger(gateway_name, trigger_config_name)
    state = tc['properties']['state']
    if state == 'Enabled':
        print(f'✅ Trigger is ACTIVE and listening for new emails!')
    else:
        print(f'⚠️  Trigger state: {state} — may take a moment to activate')

    # --- Done! ---
    print(f'\n{"=" * 60}')
    print(f'  ✅ Email Summarizer is ready!')
    print(f'{"=" * 60}')
    print(f'\n  Dashboard:    {dashboard_url}')
    print(f'  Trigger:      {trigger_config_name} (monitoring {folder_path})')
    print(f'  AOAI:         {"configured" if aoai_endpoint else "not configured"}')
    print(f'  OneDrive:     not configured (set GRAPH_TOKEN for OneDrive saves)')
    print(f'  Teams:        {"configured" if teams_url else "not configured"}')
    print(f'\n  Send yourself an email and wait 30-60 seconds for the trigger!')
    print(f'  Then check the dashboard: {dashboard_url}')


def cleanup():
    from azure.connectorgateway import ConnectorGatewayClient, TriggerClient
    from azure.sandbox import SandboxClient
    from azure.mgmt.sandbox import SandboxGroupManagementClient

    account = az_account()
    subscription_id = account['id']
    resource_group = prompt('Resource group to delete', 'lab-04-email-summarizer')
    gateway_name = prompt('Gateway name', 'email-summarizer-gw')
    sandbox_group_name = prompt('Sandbox group name', 'email-summarizer-sg')

    confirm = input(f'\nDelete ALL resources in {resource_group}? (yes/no): ').strip()
    if confirm.lower() != 'yes':
        print('Cancelled.')
        return

    conn_client = ConnectorGatewayClient(subscription_id=subscription_id, resource_group=resource_group)
    trigger_client = TriggerClient(subscription_id=subscription_id, resource_group=resource_group)
    sbx_client = SandboxClient(subscription_id=subscription_id, resource_group=resource_group)
    mgmt = SandboxGroupManagementClient(subscription_id=subscription_id, resource_group=resource_group)

    for name in ['email-trigger']:
        try:
            trigger_client.delete_trigger(gateway_name, name)
            print(f'Deleted trigger: {name}')
        except Exception:
            pass

    for sbx in sbx_client.list_sandboxes(sandbox_group_name):
        try:
            sbx_client.delete_sandbox(sbx['id'], sandbox_group_name)
            print(f'Deleted sandbox: {sbx["id"]}')
        except Exception:
            pass

    try:
        mgmt.delete_group(sandbox_group_name)
        print(f'Deleted sandbox group: {sandbox_group_name}')
    except Exception:
        pass

    try:
        conn_client.delete_gateway(gateway_name)
        print(f'Deleted gateway: {gateway_name}')
    except Exception:
        pass

    subprocess.run(
        ['az', 'group', 'delete', '--name', resource_group, '--yes', '--no-wait'],
        shell=_SHELL)
    print(f'Deleting resource group: {resource_group} (async)')
    print('✅ Cleanup complete!')


if __name__ == '__main__':
    if '--cleanup' in sys.argv:
        cleanup()
    else:
        main()
