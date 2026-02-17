#!/usr/bin/env -S uv run --with flask --with boto3
# /// script
# dependencies = ["flask", "boto3"]
# ///

import json
import os
import threading
import webbrowser
from datetime import datetime, timedelta
from flask import Flask, render_template_string, jsonify, request
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

app = Flask(__name__)

# Load HTML template from file
def load_template():
    template_path = os.path.join(os.path.dirname(__file__), 'template.html')
    with open(template_path, 'r') as f:
        return f.read()


# AWS Session Management
aws_session = None

def get_aws_session():
    """Get or create AWS session"""
    global aws_session
    if aws_session:
        return aws_session
    
    # Try environment variables first
    try:
        session = boto3.Session()
        # Test credentials
        session.client('logs').describe_log_groups(limit=1)
        aws_session = session
        return session
    except (NoCredentialsError, ClientError):
        return None

def configure_aws(access_key, secret_key, region, session_token=None):
    """Configure AWS credentials"""
    global aws_session
    try:
        session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
            region_name=region
        )
        # Test credentials
        session.client('logs').describe_log_groups(limit=1)
        aws_session = session
        return True
    except Exception as e:
        print(f"AWS configuration error: {e}")
        return False

def get_agent_list():
    """Get list of agents from CloudWatch log groups"""
    session = get_aws_session()
    if not session:
        return []
    
    try:
        logs_client = session.client('logs')
        response = logs_client.describe_log_groups(
            logGroupNamePrefix='/aws/bedrock-agentcore/runtimes/'
        )
        
        agents = []
        for log_group in response.get('logGroups', []):
            # Extract agent ID from log group name
            # Format: /aws/bedrock-agentcore/runtimes/<agent-id>-DEFAULT or <agent-id>
            name = log_group['logGroupName']
            agent_id = name.split('/')[-1]
            # Remove -DEFAULT suffix if present
            if agent_id.endswith('-DEFAULT'):
                agent_id = agent_id[:-8]
            print(f"Found agent: {agent_id} (log group: {name})")
            agents.append(agent_id)
        
        print(f"Total agents found: {len(agents)}")
        return agents
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code in ['ExpiredToken', 'InvalidClientTokenId', 'UnrecognizedClientException']:
            print(f"AWS credentials expired or invalid: {error_code}")
            # Clear the session so it can be re-initialized
            global aws_session
            aws_session = None
            raise Exception("AWS_CREDENTIALS_EXPIRED")
        raise
    except Exception as e:
        print(f"Error fetching agents: {e}")
        import traceback
        traceback.print_exc()
        return []

def query_sessions(agent_id, hours=24):
    """Query sessions from CloudWatch Logs - group by session, then by trace"""
    session = get_aws_session()
    if not session:
        return []
    
    try:
        logs_client = session.client('logs')
        log_group = f'/aws/bedrock-agentcore/runtimes/{agent_id}-DEFAULT'
        
        import time as time_module
        end_timestamp = int(time_module.time())
        start_timestamp = end_timestamp - (int(hours) * 3600)
        
        query = """
        fields @timestamp, @message
        | filter @logStream = "otel-rt-logs"
        | sort @timestamp asc
        | limit 1000
        """
        
        print(f"Querying log group: {log_group}")
        
        start_query_response = logs_client.start_query(
            logGroupName=log_group,
            startTime=start_timestamp,
            endTime=end_timestamp,
            queryString=query
        )
        
        query_id = start_query_response['queryId']
        
        import time
        response = None
        for _ in range(30):
            time.sleep(1)
            response = logs_client.get_query_results(queryId=query_id)
            if response['status'] == 'Complete':
                break
        
        if not response or response['status'] != 'Complete':
            return []
        
        print(f"Found {len(response.get('results', []))} log entries")
        
        # Parse logs and group by session -> trace
        sessions_map = {}
        trace_to_session = {}  # Map trace_id to session_id
        
        # First pass: extract session mappings from strands logs
        for result in response.get('results', []):
            fields = {field['field']: field['value'] for field in result}
            message = fields.get('@message', '')
            
            try:
                if not (isinstance(message, str) and message.strip().startswith('{')):
                    continue
                    
                log_data = json.loads(message)
                scope_name = log_data.get('scope', {}).get('name', '')
                attributes = log_data.get('attributes', {})
                trace_id = log_data.get('traceId')
                
                # Extract session ID from strands.telemetry.tracer
                if 'strands.telemetry.tracer' in scope_name:
                    session_id = attributes.get('session.id')
                    if session_id and trace_id:
                        if session_id not in sessions_map:
                            sessions_map[session_id] = {
                                'sessionId': session_id,
                                'traces': {},
                                'timestamp': fields.get('@timestamp', ''),
                                'duration': 0
                            }
                        trace_to_session[trace_id] = session_id
                        print(f"Mapped trace {trace_id[:8]} to session {session_id[:8]}")
            except json.JSONDecodeError:
                continue
        
        # Second pass: parse spans
        for result in response.get('results', []):
            fields = {field['field']: field['value'] for field in result}
            message = fields.get('@message', '')
            
            try:
                if not (isinstance(message, str) and message.strip().startswith('{')):
                    continue
                    
                log_data = json.loads(message)
                scope_name = log_data.get('scope', {}).get('name', '')
                attributes = log_data.get('attributes', {})
                event_name = attributes.get('event.name', '')
                body = log_data.get('body', {})
                trace_id = log_data.get('traceId')
                span_id = log_data.get('spanId')
                time_nano = log_data.get('timeUnixNano', 0)
                
                # Skip if no trace_id
                if not trace_id:
                    continue
                
                # Find which session this trace belongs to
                session_id = trace_to_session.get(trace_id)
                if not session_id:
                    continue
                
                sdata = sessions_map[session_id]
                
                # Initialize trace if needed
                if trace_id not in sdata['traces']:
                    sdata['traces'][trace_id] = {
                        'traceId': trace_id,
                        'spans': [],
                        'start_time': time_nano,
                        'end_time': time_nano
                    }
                
                trace = sdata['traces'][trace_id]
                trace['end_time'] = max(trace['end_time'], time_nano)
                trace['start_time'] = min(trace['start_time'], time_nano)
                
                # Deduplicate and parse spans - group by spanId
                if 'bedrock-runtime' in scope_name:
                    # Skip assistant messages (duplicates)
                    if event_name == 'gen_ai.assistant.message':
                        continue
                    
                    # Find or create span for this spanId
                    span = None
                    for s in trace['spans']:
                        if s['spanId'] == span_id:
                            span = s
                            break
                    
                    if not span:
                        span = {
                            'spanId': span_id,
                            'name': 'Processing...',
                            'type': 'llm_invocation',
                            'icon': '🤖',
                            'timestamp': time_nano,
                            'end_time': time_nano,
                            'input_messages': [],
                            'output': None,
                            'raw_events': [],
                            'tokens': {'input': 0, 'output': 0}
                        }
                        trace['spans'].append(span)
                    
                    # Update end time
                    span['end_time'] = max(span['end_time'], time_nano)
                    
                    # Add raw event
                    span['raw_events'].append({
                        'event_name': event_name,
                        'timestamp': time_nano,
                        'attributes': attributes,
                        'body': body
                    })
                    
                    # Parse event into input/output
                    if event_name == 'gen_ai.system.message':
                        text = body.get('content', [{}])[0].get('text', '')
                        span['input_messages'].append({
                            'role': 'system',
                            'content': text
                        })
                    
                    elif event_name == 'gen_ai.user.message':
                        # Check if it's a tool result or regular user message
                        content_item = body.get('content', [{}])[0]
                        if content_item.get('toolResult'):
                            # Tool result fed back to LLM
                            tool_result = content_item['toolResult']
                            result_content = tool_result.get('content', [{}])
                            result_text = result_content[0].get('text', '') if result_content else ''
                            span['input_messages'].append({
                                'role': 'tool_result',
                                'content': result_text,
                                'tool_use_id': tool_result.get('toolUseId')
                            })
                        else:
                            # Regular user message
                            text = content_item.get('text', '')
                            span['input_messages'].append({
                                'role': 'user',
                                'content': text
                            })
                    
                    elif event_name == 'gen_ai.choice':
                        message = body.get('message', {})
                        tool_calls = message.get('tool_calls', [])
                        finish_reason = body.get('finish_reason', '')
                        
                        # Extract tokens
                        span['tokens']['input'] += attributes.get('gen_ai.usage.input_tokens', 0)
                        span['tokens']['output'] += attributes.get('gen_ai.usage.output_tokens', 0)
                        
                        if tool_calls:
                            tool_name = tool_calls[0].get('function', {}).get('name', 'unknown')
                            span['name'] = f'Tool Call: {tool_name}'
                            span['icon'] = '🔧'
                            span['type'] = 'tool_call'
                            span['output'] = {
                                'type': 'tool_call',
                                'tool_name': tool_name,
                                'arguments': tool_calls[0].get('function', {}).get('arguments', {})
                            }
                        elif finish_reason == 'end_turn':
                            span['name'] = 'LLM Response'
                            span['icon'] = '🤖'
                            span['type'] = 'llm_response'
                            text = message.get('content', [{}])[0].get('text', '')
                            span['output'] = {
                                'type': 'text',
                                'content': text
                            }
                    
                    elif event_name == 'gen_ai.tool.message':
                        # Add tool result to output
                        result_text = body.get('content', [{}])[0].get('text', '')
                        if span['output'] and span['output'].get('type') == 'tool_call':
                            span['output']['result'] = result_text
                        
            except json.JSONDecodeError:
                continue
        
        # Build final session list with traces
        sessions = []
        for session_id, session_data in sessions_map.items():
            traces = []
            for trace_id, trace_data in session_data['traces'].items():
                # Sort spans by timestamp
                trace_data['spans'].sort(key=lambda x: x['timestamp'])
                
                # Build tool call history for the trace
                tool_call_history = {}  # toolUseId -> tool call info
                
                for span in trace_data['spans']:
                    # Collect tool calls from this span's output
                    if span.get('output', {}).get('type') == 'tool_call':
                        # Extract tool use ID from raw events
                        for event in span.get('raw_events', []):
                            if event['event_name'] == 'gen_ai.choice':
                                tool_calls = event['body'].get('message', {}).get('tool_calls', [])
                                if tool_calls:
                                    tool_use_id = tool_calls[0].get('id')
                                    if tool_use_id:
                                        tool_call_history[tool_use_id] = {
                                            'tool_name': tool_calls[0].get('function', {}).get('name'),
                                            'arguments': tool_calls[0].get('function', {}).get('arguments', {})
                                        }
                
                # Now inject assistant tool calls before their results in input_messages
                for span in trace_data['spans']:
                    new_input_messages = []
                    for msg in span.get('input_messages', []):
                        if msg['role'] == 'tool_result' and msg.get('tool_use_id'):
                            # Find the corresponding tool call
                            tool_call_info = tool_call_history.get(msg['tool_use_id'])
                            if tool_call_info:
                                # Insert assistant tool call before the result
                                new_input_messages.append({
                                    'role': 'assistant',
                                    'content': f"Tool Call: {tool_call_info['tool_name']}\nArguments: {json.dumps(tool_call_info['arguments'], indent=2)}"
                                })
                                # Add tool name to the result message
                                msg['tool_name'] = tool_call_info['tool_name']
                        new_input_messages.append(msg)
                    span['input_messages'] = new_input_messages
                
                # Calculate span durations and refine names
                for span in trace_data['spans']:
                    span['duration'] = round((span['end_time'] - span['timestamp']) / 1_000_000, 2)
                    
                    # Determine trigger type from last non-system message
                    trigger_msg = None
                    for msg in reversed(span.get('input_messages', [])):
                        if msg['role'] in ['user', 'tool_result']:
                            trigger_msg = msg
                            break
                    
                    # Debug logging
                    print(f"Span {span['spanId'][:8]}: trigger_msg role = {trigger_msg['role'] if trigger_msg else 'None'}, type = {span['type']}")
                    
                    # Refine span name based on trigger
                    trigger_label = ''
                    if trigger_msg:
                        if trigger_msg['role'] == 'tool_result':
                            trigger_label = 'Tool Result'
                        elif trigger_msg['role'] == 'user':
                            trigger_label = 'User'
                    
                    if span['type'] == 'tool_call' and span.get('output', {}).get('tool_name'):
                        tool_name = span['output']['tool_name']
                        span['name'] = f"LLM Invoke: {trigger_label}" if trigger_label else f"Tool Call: {tool_name}"
                        span['icon'] = '🔄' if trigger_msg and trigger_msg['role'] == 'tool_result' else '🔧'
                    elif span['type'] == 'llm_response':
                        span['name'] = f"LLM Invoke: {trigger_label}" if trigger_label else "LLM Response"
                        span['icon'] = '🔄' if trigger_msg and trigger_msg['role'] == 'tool_result' else '🤖'
                
                trace_duration = round((trace_data['end_time'] - trace_data['start_time']) / 1_000_000, 2)
                
                traces.append({
                    'traceId': trace_id,
                    'duration': trace_duration,
                    'spans': trace_data['spans']
                })
            
            sessions.append({
                'sessionId': session_id,
                'timestamp': session_data['timestamp'],
                'traceCount': len(traces),
                'traces': traces
            })
        
        sessions.sort(key=lambda x: x['timestamp'], reverse=True)
        print(f"Extracted {len(sessions)} sessions with traces")
        return sessions
        
    except Exception as e:
        print(f"Error querying sessions: {e}")
        import traceback
        traceback.print_exc()
        return []

def query_trace_details(agent_id, session_id):
    """Query detailed trace/span data for a session - data already loaded in query_sessions"""
    # Since we already load spans in query_sessions, this is just a placeholder
    # The frontend will use the spans already attached to the session
    return {'spans': []}

# Flask Routes
@app.route('/')
def index():
    return render_template_string(load_template())

@app.route('/api/check-aws')
def check_aws():
    session = get_aws_session()
    configured = session is not None
    region = session.region_name if session else os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
    return jsonify({'configured': configured, 'region': region})

@app.route('/api/configure-aws', methods=['POST'])
def configure_aws_endpoint():
    data = request.json
    success = configure_aws(
        data.get('access_key'),
        data.get('secret_key'),
        data.get('region', 'us-east-1'),
        data.get('session_token')
    )
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Invalid credentials'})

@app.route('/api/agents')
def agents():
    try:
        agent_list = get_agent_list()
        return jsonify({'agents': agent_list})
    except Exception as e:
        if str(e) == "AWS_CREDENTIALS_EXPIRED":
            return jsonify({'error': 'AWS_CREDENTIALS_EXPIRED', 'message': 'Your AWS credentials have expired. Please update them and refresh.'}), 401
        return jsonify({'error': str(e)}), 500

@app.route('/api/sessions')
def sessions():
    agent = request.args.get('agent')
    hours = request.args.get('hours', '24')
    
    if not agent:
        return jsonify({'error': 'Agent parameter required'})
    
    try:
        session_list = query_sessions(agent, hours)
        return jsonify({'sessions': session_list})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/trace')
def trace():
    agent = request.args.get('agent')
    session_id = request.args.get('session')
    
    if not agent or not session_id:
        return jsonify({'error': 'Agent and session parameters required'})
    
    try:
        trace_data = query_trace_details(agent, session_id)
        return jsonify(trace_data)
    except Exception as e:
        return jsonify({'error': str(e)})

def open_browser():
    """Open browser after a short delay"""
    webbrowser.open('http://localhost:5000')

if __name__ == '__main__':
    print("🔍 Starting YAAT...")
    print("📊 Yet Another Agent Tracer - for Bedrock Agent Core Observability")
    print("🌐 Opening browser at http://localhost:5000")
    
    # Open browser after 1.5 seconds
    threading.Timer(1.5, open_browser).start()
    
    # Start Flask app
    app.run(host='0.0.0.0', port=5000, debug=False)
