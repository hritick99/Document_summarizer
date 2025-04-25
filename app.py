import os
import io
import docx
import openai
import csv
from flask import Flask, redirect, request, session, jsonify, url_for, make_response, render_template
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PyPDF2 import PdfReader

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY')
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'  # Remove in production

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
CLIENT_SECRETS_FILE = 'client_secret.json'
FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')

@app.route('/')
def index():
    return render_template('index.html', authenticated='credentials' in session)

@app.route('/authorize')
def authorize():
    flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES)
    flow.redirect_uri = url_for('oauth2callback', _external=True)
    auth_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true')
    session['state'] = state
    return redirect(auth_url)

@app.route('/oauth2callback')
def oauth2callback():
    try:
        flow = Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE, scopes=SCOPES, state=session['state']
        )
        flow.redirect_uri = url_for('oauth2callback', _external=True)
        flow.fetch_token(authorization_response=request.url)

        credentials = flow.credentials
        session['credentials'] = credentials_to_dict(credentials)
        return redirect('/')
    except Exception as e:
        return render_template('error.html', error=f"OAuth Error: {str(e)}")

@app.route('/list-files')
def list_files():
    if 'credentials' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        creds = Credentials(**session['credentials'])
        service = build('drive', 'v3', credentials=creds)
        
        page_token = None
        all_files = []
        
        while True:
            results = service.files().list(
                q=f"'{FOLDER_ID}' in parents and trashed = false",
                pageSize=100,
                fields="nextPageToken, files(id, name, mimeType, webViewLink)",
                pageToken=page_token
            ).execute()
            
            all_files.extend(results.get('files', []))
            page_token = results.get('nextPageToken')
            
            if not page_token:
                break
                
        return jsonify(all_files)
    except Exception as e:
        return jsonify({'error': f'Failed to list files: {str(e)}'}), 500

@app.route('/view-files')
def view_files():
    if 'credentials' not in session:
        return redirect('/authorize')
    return render_template('files.html')

def extract_text_from_file(service, file_id, file_mime_type):
    request_drive = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request_drive)
    done = False
    while not done:
        status, done = downloader.next_chunk()

    fh.seek(0)
    content = ""

    if file_mime_type == 'application/pdf':
        reader = PdfReader(fh)
        content = "\n".join(page.extract_text() or '' for page in reader.pages)
    elif file_mime_type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
        doc = docx.Document(fh)
        content = "\n".join(p.text for p in doc.paragraphs)
    elif file_mime_type == 'text/plain':
        content = fh.read().decode('utf-8')
    else:
        raise ValueError(f"Unsupported file type: {file_mime_type}")

    return content

def chunk_text(text, chunk_size=2000, overlap=200):
   
    if not text:
        return []
        
    chunks = []
    i = 0
    text_length = len(text)
    
    while i < text_length:
        # Calculate end position with overlap
        end = min(i + chunk_size, text_length)
        
        # If not the last chunk, try to break at sentence or paragraph
        if end < text_length:
            # Look for sentence-ending punctuation followed by space or newline
            for j in range(min(end + 50, text_length) - 1, end - 50, -1):
                if j >= 0 and text[j] in ['.', '!', '?', '\n'] and (j+1 >= text_length or text[j+1].isspace()):
                    end = j + 1
                    break
        
        chunks.append(text[i:end])
        i = end - overlap if end < text_length else end
    
    return chunks

def summarize_text_with_openai(text_chunks):
   
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        raise ValueError("OpenAI API key is missing. Set the OPENAI_API_KEY environment variable.")
    
    client = openai.OpenAI(api_key=api_key)
    
    if len(text_chunks) == 1:
        # For single chunk documents
        prompt = f"Summarize the following document:\n\n{text_chunks[0]}"
        
        response = client.chat.completions.create(
            model='gpt-3.5-turbo',
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=300
        )
        
        return response.choices[0].message.content
    else:
        # For multi-chunk documents
        chunk_summaries = []
        
        # First, summarize each chunk
        for i, chunk in enumerate(text_chunks):
            prompt = f"This is part {i+1} of {len(text_chunks)} of a document. Summarize this section concisely:\n\n{chunk}"
            
            response = client.chat.completions.create(
                model='gpt-3.5-turbo',
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=250
            )
            
            chunk_summaries.append(response.choices[0].message.content)
        
        # Then, create a final summary from the chunk summaries
        combined_prompt = "Create a coherent, comprehensive summary of this document based on these section summaries:\n\n"
        combined_prompt += "\n\n".join([f"Section {i+1}: {summary}" for i, summary in enumerate(chunk_summaries)])
        
        final_response = client.chat.completions.create(
            model='gpt-3.5-turbo',
            messages=[{'role': 'user', 'content': combined_prompt}],
            max_tokens=350
        )
        
        return final_response.choices[0].message.content

@app.route('/summarize/<file_id>')
def summarize(file_id):
    if 'credentials' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        creds = Credentials(**session['credentials'])
        service = build('drive', 'v3', credentials=creds)

        file_metadata = service.files().get(fileId=file_id, fields="name, mimeType, webViewLink").execute()
        
        # Extract text from file
        try:
            content = extract_text_from_file(service, file_id, file_metadata['mimeType'])
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        
        if len(content.strip()) == 0:
            return jsonify({'error': 'No extractable content found'}), 400
        
        # Chunk the document if it's large
        chunks = chunk_text(content)
        
        # Summarize content
        summary = summarize_text_with_openai(chunks)
        
        return jsonify({
            'name': file_metadata['name'],
            'link': file_metadata.get('webViewLink', ''),
            'summary': summary
        })
        
    except Exception as e:
        return jsonify({'error': f'Failed to summarize: {str(e)}'}), 500

@app.route('/summarize-all')
def summarize_all():
    if 'credentials' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        creds = Credentials(**session['credentials'])
        service = build('drive', 'v3', credentials=creds)
        
        results = service.files().list(
            q=f"'{FOLDER_ID}' in parents and trashed = false",
            fields="files(id, name, mimeType, webViewLink)"
        ).execute()
        
        files = results.get('files', [])
        summaries = []
        
        for file in files:
            # Process only supported file types
            if file['mimeType'] in ['application/pdf', 
                                  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                                  'text/plain']:
                try:
                    content = extract_text_from_file(service, file['id'], file['mimeType'])
                    
                    if len(content.strip()) > 0:
                        chunks = chunk_text(content)
                        summary = summarize_text_with_openai(chunks)
                        
                        summaries.append({
                            'id': file['id'],
                            'name': file['name'],
                            'link': file.get('webViewLink', ''),
                            'summary': summary
                        })
                except Exception as e:
                    # Log the error but continue with other files
                    print(f"Error processing file {file['name']}: {str(e)}")
                    
        return jsonify(summaries)
        
    except Exception as e:
        return jsonify({'error': f'Failed to summarize files: {str(e)}'}), 500

@app.route('/download-summary/<file_id>')
def download_summary(file_id):
    if 'credentials' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        creds = Credentials(**session['credentials'])
        service = build('drive', 'v3', credentials=creds)

        file_metadata = service.files().get(fileId=file_id, fields="name, mimeType").execute()
        
        # Extract and summarize content
        content = extract_text_from_file(service, file_id, file_metadata['mimeType'])
        
        if len(content.strip()) == 0:
            return jsonify({'error': 'No extractable content found'}), 400
        
        chunks = chunk_text(content)
        summary = summarize_text_with_openai(chunks)
        
        # Create a CSV file in memory
        csv_data = io.StringIO()
        writer = csv.writer(csv_data)
        
        # Write header
        writer.writerow(['Filename', 'Summary'])
        
        # Write data row
        writer.writerow([file_metadata['name'], summary])
        
        # Prepare response
        csv_output = csv_data.getvalue()
        csv_data.close()
        
        # Create Flask response with CSV file
        response = make_response(csv_output)
        response.headers["Content-Disposition"] = f"attachment; filename={file_metadata['name']}_summary.csv"
        response.headers["Content-type"] = "text/csv"
        
        return response
        
    except Exception as e:
        return jsonify({'error': f'Failed to download summary: {str(e)}'}), 500

@app.route('/download-all-summaries')
def download_all_summaries():
    if 'credentials' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        creds = Credentials(**session['credentials'])
        service = build('drive', 'v3', credentials=creds)
        
        results = service.files().list(
            q=f"'{FOLDER_ID}' in parents and trashed = false",
            fields="files(id, name, mimeType)"
        ).execute()
        
        files = results.get('files', [])
        
        # Create a CSV file in memory
        csv_data = io.StringIO()
        writer = csv.writer(csv_data)
        
        # Write header
        writer.writerow(['Filename', 'Summary'])
        
        # Process each file
        for file in files:
            if file['mimeType'] in ['application/pdf', 
                                  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                                  'text/plain']:
                try:
                    content = extract_text_from_file(service, file['id'], file['mimeType'])
                    
                    if len(content.strip()) > 0:
                        chunks = chunk_text(content)
                        summary = summarize_text_with_openai(chunks)
                        writer.writerow([file['name'], summary])
                except Exception as e:
                    # Log the error but continue with other files
                    print(f"Error processing file {file['name']}: {str(e)}")
                    writer.writerow([file['name'], f"Error: Could not process file"])
        
        # Prepare response
        csv_output = csv_data.getvalue()
        csv_data.close()
        
        # Create Flask response with CSV file
        response = make_response(csv_output)
        response.headers["Content-Disposition"] = "attachment; filename=document_summaries.csv"
        response.headers["Content-type"] = "text/csv"
        
        return response
        
    except Exception as e:
        return jsonify({'error': f'Failed to download summaries: {str(e)}'}), 500

def credentials_to_dict(creds):
    return {
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': creds.scopes
    }

if __name__ == '__main__':
    app.run('localhost', 5000, debug=True)