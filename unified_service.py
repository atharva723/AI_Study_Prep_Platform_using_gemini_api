from flask import Flask, request, jsonify
from flask_cors import CORS
import fitz
import os
import uuid
from datetime import datetime
import sqlite3
from werkzeug.utils import secure_filename
import google.generativeai as genai
import json

app = Flask(__name__)
CORS(app)

# Configuration
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

# GEMINI API KEY - Try multiple sources
try:
    from config import GEMINI_API_KEY
    print("✅ API key loaded from config.py")
except ImportError:
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', 'YOUR_API_KEY_HERE')
    if GEMINI_API_KEY == 'YOUR_API_KEY_HERE':
        print("⚠️  No API key found - create config.py with your key")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Initialize Gemini
if GEMINI_API_KEY != 'YOUR_API_KEY_HERE':
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-2.5-flash')
        print("✅ Gemini AI initialized (gemini-2.5-flash)")
    except Exception as e:
        print(f"⚠️  Gemini initialization failed: {e}")
        gemini_model = None
else:
    gemini_model = None
    print("⚠️  Gemini API key not set")

# Database setup
def init_db():
    conn = sqlite3.connect('platform.db')
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS content (
            content_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            extracted_text TEXT,
            page_count INTEGER,
            uploaded_at TEXT,
            status TEXT
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS questions (
            question_id TEXT PRIMARY KEY,
            content_id TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            question TEXT NOT NULL,
            option_a TEXT NOT NULL,
            option_b TEXT NOT NULL,
            option_c TEXT NOT NULL,
            option_d TEXT NOT NULL,
            correct_answer TEXT NOT NULL,
            created_at TEXT
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS attempts (
            attempt_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            question_id TEXT NOT NULL,
            selected_answer TEXT,
            is_correct INTEGER,
            attempted_at TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# Helper functions
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_pdf(file_path):
    try:
        doc = fitz.open(file_path)
        text = ""
        page_count = len(doc)
        for page_num in range(page_count):
            page = doc[page_num]
            text += page.get_text()
        doc.close()
        return text, page_count, None
    except Exception as e:
        return None, 0, str(e)

def generate_mcqs_with_gemini(text: str, difficulty: str, count: int):
    """Generate MCQs using Gemini AI"""
    if not gemini_model:
        raise Exception("Gemini API not configured")
    
    difficulty_instructions = {
        "easy": "Focus on basic definitions and simple recall. Questions should be straightforward.",
        "medium": "Test understanding and application. Questions should require connecting concepts.",
        "hard": "Test deep analysis and synthesis. Questions should be challenging and thought-provoking."
    }
    
    # Limit text for API
    text_sample = text[:3500] if len(text) > 3500 else text
    
    prompt = f"""Generate {count} multiple-choice questions from this text.

Difficulty Level: {difficulty}

Text:
{text_sample}

You must return ONLY a JSON array with this structure:
[
  {{"question": "What is X?", "options": {{"A": "opt1", "B": "opt2", "C": "opt3", "D": "opt4"}}, "correct_answer": "A", "difficulty": "{difficulty}"}}
]

Generate exactly {count} questions. Return ONLY the JSON array, nothing else."""

    try:
        print(f"\n{'='*60}")
        print(f"📤 Generating with Gemini (gemini-2.5-flash)")
        print(f"   Text length: {len(text_sample)} chars")
        print(f"   Difficulty: {difficulty}")
        print(f"   Count: {count}")
        print(f"{'='*60}\n")
        
        response = gemini_model.generate_content(prompt)
        
        # Check if response is blocked
        if not response.text:
            print("⚠️ Empty response from Gemini")
            print(f"Response object: {response}")
            
            # Check for safety ratings
            if hasattr(response, 'prompt_feedback'):
                print(f"Prompt feedback: {response.prompt_feedback}")
            
            raise Exception("Gemini returned empty response - content may be blocked")
        
        response_text = response.text.strip()
        print(f"📥 Received response ({len(response_text)} chars)")
        print(f"First 300 chars:\n{response_text[:300]}\n")
        
        # Clean markdown code blocks
        original_text = response_text
        if '```json' in response_text:
            response_text = response_text.split('```json')[1].split('```')[0].strip()
            print("✂️ Removed ```json markers")
        elif '```' in response_text:
            response_text = response_text.split('```')[1].split('```')[0].strip()
            print("✂️ Removed ``` markers")
        
        # Try to parse JSON
        try:
            questions = json.loads(response_text)
            
            if isinstance(questions, list):
                print(f"✅ Parsed as list: {len(questions)} questions")
                return questions
            elif isinstance(questions, dict) and 'questions' in questions:
                print(f"✅ Parsed as dict: {len(questions['questions'])} questions")
                return questions['questions']
            else:
                print(f"⚠️ Unexpected format: {type(questions)}")
                print(f"Content: {questions}")
                return []
                
        except json.JSONDecodeError as e:
            print(f"❌ JSON parsing failed: {e}")
            print(f"\nAttempted to parse:\n{response_text[:500]}\n")
            
            # Try to extract JSON array with regex
            import re
            json_match = re.search(r'\[\s*\{.*?\}\s*\]', response_text, re.DOTALL)
            if json_match:
                try:
                    extracted = json_match.group()
                    print(f"🔍 Found JSON pattern, trying to parse...")
                    questions = json.loads(extracted)
                    print(f"✅ Recovered {len(questions)} questions")
                    return questions
                except Exception as ex:
                    print(f"❌ Extraction failed: {ex}")
            
            # Last resort: save the response for debugging
            with open('gemini_debug_response.txt', 'w', encoding='utf-8') as f:
                f.write(f"Original:\n{original_text}\n\n")
                f.write(f"Cleaned:\n{response_text}")
            print("💾 Saved response to gemini_debug_response.txt for debugging")
            
            raise Exception(f"Could not parse JSON from response")
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}\n")
        import traceback
        traceback.print_exc()
        raise Exception(f"AI generation failed: {str(e)}")

# API Endpoints

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "service": "Interview Prep Platform - AI Powered",
        "version": "3.0",
        "ai": "Gemini" if gemini_model else "Disabled",
        "status": "running"
    }), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "healthy",
        "gemini": "enabled" if gemini_model else "disabled"
    }), 200

@app.route('/upload', methods=['POST'])
def upload_pdf():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
    
    user_id = request.form.get('user_id')
    if not user_id:
        return jsonify({"error": "User ID required"}), 400
    
    if not allowed_file(file.filename):
        return jsonify({"error": "Only PDF files allowed"}), 400
    
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)
    
    if file_size > MAX_FILE_SIZE:
        return jsonify({"error": "File too large. Max 10MB"}), 400
    
    try:
        content_id = str(uuid.uuid4())
        filename = secure_filename(file.filename)
        file_path = os.path.join(UPLOAD_FOLDER, f"{content_id}_{filename}")
        file.save(file_path)
        
        extracted_text, page_count, error = extract_text_from_pdf(file_path)
        if error:
            return jsonify({"error": f"Failed to extract text: {error}"}), 500
        
        conn = sqlite3.connect('platform.db')
        c = conn.cursor()
        c.execute('''
            INSERT INTO content (content_id, user_id, file_name, file_path, 
                               extracted_text, page_count, uploaded_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (content_id, user_id, filename, file_path, extracted_text, 
              page_count, datetime.now().isoformat(), 'processed'))
        conn.commit()
        conn.close()
        
        return jsonify({
            "success": True,
            "content_id": content_id,
            "file_name": filename,
            "page_count": page_count,
            "text_length": len(extracted_text)
        }), 201
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/generate', methods=['POST'])
def generate_questions():
    if not gemini_model:
        return jsonify({
            "error": "Gemini API not configured. Please set GEMINI_API_KEY environment variable or edit the code"
        }), 503
    
    data = request.json
    content_id = data.get('content_id')
    difficulty = data.get('difficulty', 'medium')
    count = data.get('count', 10)
    
    if not content_id:
        return jsonify({"error": "content_id required"}), 400
    
    if difficulty not in ['easy', 'medium', 'hard']:
        return jsonify({"error": "Invalid difficulty"}), 400
    
    conn = sqlite3.connect('platform.db')
    c = conn.cursor()
    c.execute('SELECT extracted_text FROM content WHERE content_id = ?', (content_id,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        return jsonify({"error": "Content not found"}), 404
    
    text = row[0]
    
    if len(text) < 100:
        return jsonify({"error": "Text too short to generate meaningful questions"}), 400
    
    try:
        print(f"\n{'='*60}")
        print(f"Generating {count} {difficulty} questions with Gemini AI...")
        print(f"Text length: {len(text)} characters")
        print(f"{'='*60}\n")
        
        mcqs = generate_mcqs_with_gemini(text, difficulty, count)
        
        if not mcqs:
            return jsonify({"error": "AI returned empty response. Try again or check your text content."}), 500
        
        print(f"✅ Generated {len(mcqs)} questions\n")
        
        conn = sqlite3.connect('platform.db')
        c = conn.cursor()
        question_ids = []
        
        for q in mcqs:
            question_id = str(uuid.uuid4())
            question_ids.append(question_id)
            c.execute('''
                INSERT INTO questions (question_id, content_id, difficulty, question,
                                     option_a, option_b, option_c, option_d, 
                                     correct_answer, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (question_id, content_id, q.get('difficulty', difficulty), q['question'],
                  q['options']['A'], q['options']['B'], q['options']['C'], 
                  q['options']['D'], q['correct_answer'], datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            "success": True,
            "content_id": content_id,
            "difficulty": difficulty,
            "generated_count": len(mcqs),
            "question_ids": question_ids,
            "ai_powered": True,
            "message": f"Generated {len(mcqs)} high-quality questions using Gemini AI!"
        }), 201
        
    except Exception as e:
        error_msg = str(e)
        print(f"\n❌ ERROR: {error_msg}\n")
        return jsonify({"error": error_msg}), 500

@app.route('/quiz', methods=['GET'])
def get_quiz():
    content_id = request.args.get('content_id')
    difficulty = request.args.get('difficulty')
    count = int(request.args.get('count', 10))
    
    if not content_id:
        return jsonify({"error": "content_id required"}), 400
    
    conn = sqlite3.connect('platform.db')
    c = conn.cursor()
    
    if difficulty:
        c.execute('''
            SELECT question_id, difficulty, question, option_a, option_b, 
                   option_c, option_d
            FROM questions 
            WHERE content_id = ? AND difficulty = ?
            ORDER BY RANDOM()
            LIMIT ?
        ''', (content_id, difficulty, count))
    else:
        c.execute('''
            SELECT question_id, difficulty, question, option_a, option_b, 
                   option_c, option_d
            FROM questions 
            WHERE content_id = ?
            ORDER BY RANDOM()
            LIMIT ?
        ''', (content_id, count))
    
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return jsonify({"error": "No questions found. Generate questions first."}), 404
    
    questions = []
    for row in rows:
        questions.append({
            "question_id": row[0],
            "difficulty": row[1],
            "question": row[2],
            "options": {
                "A": row[3],
                "B": row[4],
                "C": row[5],
                "D": row[6]
            }
        })
    
    return jsonify({
        "content_id": content_id,
        "difficulty": difficulty,
        "count": len(questions),
        "questions": questions
    }), 200

@app.route('/submit', methods=['POST'])
def submit_answer():
    data = request.json
    user_id = data.get('user_id')
    question_id = data.get('question_id')
    selected_answer = data.get('selected_answer')
    
    if not all([user_id, question_id, selected_answer]):
        return jsonify({"error": "Missing required fields"}), 400
    
    conn = sqlite3.connect('platform.db')
    c = conn.cursor()
    c.execute('SELECT correct_answer FROM questions WHERE question_id = ?', (question_id,))
    row = c.fetchone()
    
    if not row:
        conn.close()
        return jsonify({"error": "Question not found"}), 404
    
    correct_answer = row[0]
    is_correct = selected_answer.upper() == correct_answer.upper()
    
    attempt_id = str(uuid.uuid4())
    c.execute('''
        INSERT INTO attempts (attempt_id, user_id, question_id, selected_answer, 
                            is_correct, attempted_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (attempt_id, user_id, question_id, selected_answer, int(is_correct), 
          datetime.now().isoformat()))
    
    conn.commit()
    conn.close()
    
    return jsonify({
        "attempt_id": attempt_id,
        "is_correct": is_correct,
        "correct_answer": correct_answer if not is_correct else None,
        "message": "Correct! ✅" if is_correct else "Incorrect ❌"
    }), 200

if __name__ == '__main__':
    print("🚀 Interview Prep Platform - AI Powered")
    print("📍 Running on: http://localhost:5000")
    
    if gemini_model:
        print("🤖 Gemini AI: ENABLED ✅")
    else:
        print("⚠️  Gemini AI: NOT CONFIGURED")
        print("   Set GEMINI_API_KEY environment variable")
        print("   Get key from: https://makersuite.google.com/app/apikey")
    
    print("\n✅ Ready!\n")
    app.run(host='0.0.0.0', port=5000, debug=True)