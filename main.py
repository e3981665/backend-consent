import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict

from fastapi import Body, FastAPI, Form, HTTPException, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from intellisign_client import IntellisignClient, IntellisignAPIError

# ------------------ Configuração básica ------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("consent-backend")

UPLOAD_ROOT = Path(os.getenv("UPLOAD_ROOT", "/app/uploads"))

INTELLISIGN_BASE_URL = os.getenv("INTELLISIGN_BASE_URL", "https://api.intellisign.com")
INTELLISIGN_CLIENT_ID = os.getenv("INTELLISIGN_CLIENT_ID", "")
INTELLISIGN_CLIENT_SECRET = os.getenv("INTELLISIGN_CLIENT_SECRET", "")
INTELLISIGN_SCOPE = os.getenv("INTELLISIGN_SCOPE", "*")

SIGNER_NAME_DEFAULT = os.getenv("SIGNER_NAME_DEFAULT", "User")
SIGNER_EMAIL_DEFAULT = os.getenv("SIGNER_EMAIL_DEFAULT", "test@example.com")

client = IntellisignClient(
    base_url=INTELLISIGN_BASE_URL,
    client_id=INTELLISIGN_CLIENT_ID,
    client_secret=INTELLISIGN_CLIENT_SECRET,
    scope=INTELLISIGN_SCOPE,
)

app = FastAPI(title="Consent Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # em produção, restringir ao domínio do Base44
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Memória simples: documentId -> info de status
CONSENT_STORE: Dict[str, Dict] = {}


class SendConsentRequest(BaseModel):
    email: str
    content: str | None = None
    consentId: str


# ------------------ Models ------------------

class ConsentStatusResponse(BaseModel):
    status: str
    documentId: str
    consentId: str
    envelopeId: str | None = None
    signedAt: datetime | None = None
    downloadAvailable: bool = False
    downloadUrl: str | None = None


# ------------------ Util: gerar PDF do texto ------------------

def generate_pdf_from_text(content: str, output_path: Path):
    """
    Gera um PDF simples com o texto do termo.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    output_path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(output_path), pagesize=A4)
    width, height = A4

    # Margens simples
    x = 50
    y = height - 50
    max_width = width - 100

    # Quebrar em linhas simples
    from textwrap import wrap

    lines = []
    for paragraph in content.split("\n"):
        wrapped = wrap(paragraph, width=100) or [""]
        lines.extend(wrapped)

    c.setFont("Helvetica", 11)
    for line in lines:
        if y < 50:
            c.showPage()
            c.setFont("Helvetica", 11)
            y = height - 50
        c.drawString(x, y, line)
        y -= 14

    c.save()


# ------------------ Endpoints ------------------

@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/consents/send", response_model=ConsentStatusResponse)
async def send_consent(
    email: str | None = Form(None),
    consentId: str | None = Form(None),
    content: str | None = Form(None),
    file: UploadFile | None = File(None),
    json_payload: SendConsentRequest | None = Body(None),
):
    """
    Recebe via multipart/form-data:
      - email do usuário (signatário)
      - consentId (ID lógico no app)
      - file (PDF) OU content (texto) para gerar o PDF
    Cria envelope no Intellisign, adiciona signatário e envia.
    """
    if not INTELLISIGN_CLIENT_ID or not INTELLISIGN_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Intellisign não configurado")

    # Permitir JSON (content-type application/json) ou multipart com arquivo/texto
    if json_payload:
        email = email or json_payload.email
        consentId = consentId or json_payload.consentId
        content = content or json_payload.content

    if not email or not consentId:
        raise HTTPException(status_code=400, detail="Campos obrigatórios: email, consentId")

    document_id = str(uuid.uuid4())
    logger.info("Preparando documento para consentId=%s, documentId=%s", consentId, document_id)

    if not file and not content:
        raise HTTPException(status_code=400, detail="Envie um PDF em 'file' ou texto em 'content'")

    # 1) Armazenar PDF local (arquivo enviado ou gerado do texto)
    base_dir = UPLOAD_ROOT / document_id
    original_dir = base_dir / "original"
    signed_dir = base_dir / "signed"
    pdf_path = original_dir / "consent.pdf"
    original_dir.mkdir(parents=True, exist_ok=True)

    if file:
        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Apenas PDF é aceito em 'file'")
        pdf_path = original_dir / (file.filename or "consent.pdf")
        contents = await file.read()
        with pdf_path.open("wb") as dest:
            dest.write(contents)
    else:
        generate_pdf_from_text(content or "", pdf_path)

    try:
        # 2) Token Intellisign
        token = client.get_access_token()

        # 3) Envelope
        subject = f"Consentimento - {consentId}"
        message = "Por favor, assine o termo de consentimento enviado pelo sistema."
        envelope_id = client.create_envelope(token, name=subject, subject=subject, message=message)

        # 4) Upload do PDF
        client.add_document(token, envelope_id, pdf_path, filename=pdf_path.name)

        # 5) Adicionar signatário (email do usuário conectado)
        client.add_recipient(
            token,
            envelope_id,
            name=SIGNER_NAME_DEFAULT,  # se quiser, pode mandar o nome no payload depois
            email=email,
            signature_type="simple",
        )

        # 6) Enviar envelope
        client.send_envelope(token, envelope_id)

    except IntellisignAPIError as e:
        logger.exception("Falha ao enviar envelope para Intellisign")
        raise HTTPException(status_code=502, detail=str(e))

    # Guardar estado em memória
    CONSENT_STORE[document_id] = {
        "consentId": consentId,
        "email": email,
        "envelopeId": envelope_id,
        "status": "sent",
        "signedAt": None,
        "downloadAvailable": False,
        "signedFile": signed_dir / "signed.pdf",
    }

    return ConsentStatusResponse(
        status="sent",
        documentId=document_id,
        consentId=consentId,
        envelopeId=envelope_id,
        signedAt=None,
        downloadAvailable=False,
    )


@app.get("/api/consents/{document_id}/status", response_model=ConsentStatusResponse)
def get_consent_status(document_id: str, request: Request):
    """
    Consulta status local + status do envelope no Intellisign.
    (Aqui, para simplificar, vamos assumir que se o envelope estiver 'completed',
    fazemos o download do PDF final e marcamos como concluído.)
    """
    info = CONSENT_STORE.get(document_id)
    if not info:
        raise HTTPException(status_code=404, detail="Documento não encontrado")

    status = info["status"]
    envelope_id = info["envelopeId"]

    # Se ainda não completou, consulta Intellisign
    if status != "completed":
        try:
            token = client.get_access_token()
            env_data = client.get_envelope_status(token, envelope_id)
            env_status = (env_data.get("status") or "").lower()
            # Ajuste esta lógica conforme o campo real usado por Intellisign
            if env_status in ("completed", "signed", "finished"):
                # Baixa o PDF final
                signed_path: Path = info["signedFile"]
                client.download_completed_document(token, envelope_id, signed_path)
                info["status"] = "completed"
                info["downloadAvailable"] = True
                info["signedAt"] = datetime.utcnow()
                status = "completed"
        except IntellisignAPIError as e:
            # Não falhar duro na consulta de status, apenas logar
            logger.warning("Erro ao consultar envelope no Intellisign: %s", e)

    download_url = None
    if info.get("downloadAvailable"):
        download_url = str(request.url_for("download_consent", document_id=document_id))

    return ConsentStatusResponse(
        status=status,
        documentId=document_id,
        consentId=info["consentId"],
        envelopeId=envelope_id,
        signedAt=info.get("signedAt"),
        downloadAvailable=bool(info.get("downloadAvailable")),
        downloadUrl=download_url,
    )


@app.get("/api/consents/{document_id}/download")
def download_consent(document_id: str):
    """
    Faz o download do PDF assinado, se já estiver disponível.
    """
    info = CONSENT_STORE.get(document_id)
    if not info:
        raise HTTPException(status_code=404, detail="Documento não encontrado")

    if info.get("status") != "completed":
        raise HTTPException(status_code=400, detail="Documento ainda não está assinado")

    signed_path: Path = info["signedFile"]
    if not signed_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo assinado ainda não disponível")

    filename = f"consent_{info['consentId']}.pdf"
    return FileResponse(
        path=signed_path,
        media_type="application/pdf",
        filename=filename,
    )


# Depois você pode evoluir isso pra salvar em banco ou em bucket (GCS) em vez de disk local, mas pra MVP no Cloud Run já funciona (pasta /app/uploads).
