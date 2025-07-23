import streamlit as st
import requests
import json
import os

API_URL = os.getenv("API_URL", "http://localhost:8000")

st.title("VozMulti TTS Web")

st.sidebar.header("Proyecto")
project_name = st.sidebar.text_input("Nombre del proyecto", value="demo")
uploaded_file = st.sidebar.file_uploader("Sube tu archivo de texto (.txt)", type=["txt"])

if uploaded_file is not None:
    with st.spinner("Subiendo archivo..."):
        files = {"file": uploaded_file}
        data = {"projectname": project_name}
        r = requests.post(f"{API_URL}/upload_text/", files=files, data=data)
        st.success("Archivo subido")

if st.button("Cargar voces disponibles"):
    r = requests.get(f"{API_URL}/list_voices/")
    if r.ok:
        voices = r.json()
        st.session_state["voices"] = voices
        st.write("Voces disponibles:", [v["name"] for v in voices])
    else:
        st.error("No se pudieron cargar las voces")

if "voices" in st.session_state:
    st.header("Asignar voces")
    narrator_voice = st.selectbox("Voz para Narrador", [v["name"] for v in st.session_state["voices"]])
    # Puedes añadir más roles y asignaciones aquí

    if st.button("Sintetizar audio"):
        text_filename = uploaded_file.name if uploaded_file else None
        if not text_filename:
            st.error("Sube primero un archivo de texto")
        else:
            voice_assignments = json.dumps({"Narrator": narrator_voice})
            data = {"projectname": project_name, "text_filename": text_filename, "voice_assignments": voice_assignments}
            r = requests.post(f"{API_URL}/synthesize/", data=data)
            if r.ok:
                st.success("Audio generado")
                st.audio(f"{API_URL}/download_audio/?projectname={project_name}")
            else:
                st.error("Error en la síntesis")
