import os
import re
import json
from pydub import AudioSegment
from fastapi import FastAPI, File, UploadFile, Form
from fastapi.responses import FileResponse, JSONResponse
from google.cloud import texttospeech

BYTE_LIMIT = 4800
PARTS_DIR = "projects"
PARAGRAPH_SEP = "\n\n"
ASSIGNMENTS_SUFFIX = "_voces.txt"
METADATA_SUFFIX = "_metadata.json"
DIALOGUE_PATTERN = re.compile(r'\{([^}]*)\}\s*\[(.*?)\]', re.DOTALL)

app = FastAPI()

def chunk_text(text, limit=BYTE_LIMIT):
    if text.strip().startswith('<speak>'):
        return [text]
    chunks = []
    current_chunk = ""
    if not text or not text.strip(): return []
    text = re.sub(r'\n\s*\n', PARAGRAPH_SEP, text).strip()
    paragraphs = text.split(PARAGRAPH_SEP)
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph: continue
        paragraph_bytes = len(paragraph.encode('utf-8'))
        if paragraph_bytes > limit:
            if current_chunk: chunks.append(current_chunk); current_chunk = ""
            chunks.extend(split_oversized_paragraph_by_sentence(paragraph, limit))
            current_chunk = ""
            continue
        bytes_needed = paragraph_bytes
        if current_chunk: bytes_needed += len(PARAGRAPH_SEP.encode('utf-8'))
        if len(current_chunk.encode('utf-8')) + bytes_needed <= limit:
            current_chunk += PARAGRAPH_SEP + paragraph if current_chunk else paragraph
        else:
            if current_chunk: chunks.append(current_chunk)
            current_chunk = paragraph
    if current_chunk: chunks.append(current_chunk)
    return [c for c in chunks if c.strip()]

def split_oversized_paragraph_by_sentence(paragraph, limit):
    sub_chunks, current_sub_chunk = [], ""
    sentence_end_pattern = re.compile(r'(?<=[.!?])(\s*\n?|\s+)')
    last_end, sentences = 0, []
    for match in sentence_end_pattern.finditer(paragraph):
        sentences.append(paragraph[last_end:match.start()] + match.group(1)); last_end = match.end()
    if paragraph[last_end:].strip(): sentences.append(paragraph[last_end:])
    if not sentences: return split_by_words(paragraph.strip(), limit)
    for sentence in sentences:
        if len((current_sub_chunk + sentence).encode('utf-8')) > limit and current_sub_chunk:
            sub_chunks.append(current_sub_chunk.strip()); current_sub_chunk = ""
        current_sub_chunk += sentence
    if current_sub_chunk: sub_chunks.append(current_sub_chunk.strip())
    return [sc for sc in sub_chunks if sc]

def split_by_words(text, limit):
    word_chunks, current_chunk = [], ""
    for word in re.findall(r'\S+|\s+', text):
        if len((current_chunk + word).encode('utf-8')) > limit and current_chunk:
            word_chunks.append(current_chunk.strip()); current_chunk = ""
        current_chunk += word
    if current_chunk: word_chunks.append(current_chunk.strip())
    return [wc for wc in word_chunks if wc]

def parse_text_with_markers(text):
    segments, last_end = [], 0
    for match in DIALOGUE_PATTERN.finditer(text):
        if narrator_text := text[last_end:match.start()].strip(): segments.append({'type': 'narrator', 'text': narrator_text})
        character, dialogue = match.group(1).strip(), match.group(2)
        if character: segments.append({'type': 'dialogue', 'character': character, 'text': dialogue})
        else: segments.append({'type': 'narrator', 'text': match.group(0)})
        last_end = match.end()
    if remaining_text := text[last_end:].strip(): segments.append({'type': 'narrator', 'text': remaining_text})
    if not segments and text.strip(): segments.append({'type': 'narrator', 'text': text})
    return segments

def synthesize_text_to_speech(text_content, voice_name, audio_output_filename):
    client = texttospeech.TextToSpeechClient()
    cleaned_text = text_content.strip()
    if cleaned_text.startswith('<speak>') and cleaned_text.endswith('</speak>'):
        synthesis_input = texttospeech.SynthesisInput(ssml=text_content)
    else:
        synthesis_input = texttospeech.SynthesisInput(text=text_content)
    parts = voice_name.split('-')
    language_code = f"{parts[0]}-{parts[1]}" if len(parts) >= 2 and len(f"{parts[0]}-{parts[1]}") == 5 else 'en-US'
    voice_params = texttospeech.VoiceSelectionParams(language_code=language_code, name=voice_name)
    audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
    response = client.synthesize_speech(input=synthesis_input, voice=voice_params, audio_config=audio_config)
    with open(audio_output_filename, "wb") as out_file: out_file.write(response.audio_content)

def merge_audio_parts(part_files, output_filename):
    combined = AudioSegment.empty()
    for part_file in part_files:
        if not os.path.exists(part_file) or os.path.getsize(part_file) == 0:
            continue
        combined += AudioSegment.from_mp3(part_file)
    if len(combined) == 0:
        return False
    combined.export(output_filename, format="mp3")
    return True

@app.post("/upload_text/")
async def upload_text(projectname: str = Form(...), file: UploadFile = File(...)):
    os.makedirs(PARTS_DIR, exist_ok=True)
    project_dir = os.path.join(PARTS_DIR, projectname)
    os.makedirs(project_dir, exist_ok=True)
    filepath = os.path.join(project_dir, file.filename)
    with open(filepath, "wb") as f:
        f.write(await file.read())
    return JSONResponse({"status": "ok", "filename": file.filename})

@app.get("/list_voices/")
def list_voices():
    client = texttospeech.TextToSpeechClient()
    response = client.list_voices()
    voices = []
    for voice in response.voices:
        voices.append({"name": voice.name, "language_codes": voice.language_codes})
    return voices

@app.post("/synthesize/")
def synthesize(projectname: str, text_filename: str, voice_assignments: str):
    # voice_assignments: json {"Narrator": "es-ES-Wavenet-A", ...}
    project_dir = os.path.join(PARTS_DIR, projectname)
    filepath = os.path.join(project_dir, text_filename)
    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read()
    segments = parse_text_with_markers(text)
    voice_map = json.loads(voice_assignments)
    all_audio_files = []
    metadata = {}
    total_parts = sum(len(chunk_text(s['text'], BYTE_LIMIT)) for s in segments if s.get('text','').strip())
    part_counter = 0
    for seg_idx, segment in enumerate(segments, 1):
        role = segment.get('character', 'Narrator')
        text = segment.get('text', '').strip()
        if not text:
            continue
        voice = voice_map.get(role)
        for chk_idx, chunk_content in enumerate(chunk_text(text, BYTE_LIMIT), 1):
            part_counter += 1
            part_base = f"{projectname}_seg{seg_idx:04d}_chunk{chk_idx:03d}"
            audio_file = os.path.join(project_dir, f"{part_base}.mp3")
            synthesize_text_to_speech(chunk_content, voice, audio_file)
            all_audio_files.append(audio_file)
            metadata[part_base] = {"voice": voice, "role": role}
    output_filename = os.path.join(project_dir, f"{projectname}_output.mp3")
    merge_audio_parts(all_audio_files, output_filename)
    # Save metadata
    with open(os.path.join(project_dir, f"{projectname}{METADATA_SUFFIX}"), "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=4)
    return {"status": "ok", "audio_file": f"{projectname}_output.mp3"}

@app.get("/download_audio/")
def download_audio(projectname: str):
    project_dir = os.path.join(PARTS_DIR, projectname)
    output_filename = os.path.join(project_dir, f"{projectname}_output.mp3")
    if os.path.exists(output_filename):
        return FileResponse(output_filename, media_type="audio/mpeg", filename=f"{projectname}_output.mp3")
    return {"status": "not found"}