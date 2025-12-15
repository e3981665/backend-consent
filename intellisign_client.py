from pathlib import Path
from typing import Any, Dict, Optional

import requests


class IntellisignAPIError(Exception):
    pass


class IntellisignClient:
    """
    Cliente fino para a API do Intellisign.

    Usa OAuth2 client_credentials para pegar um access_token e depois
    chama endpoints /v1 de envelopes e documentos.
    """

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        scope: str = "*",
    ):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope

    # ---------- Auth ----------

    def get_access_token(self) -> str:
        """
        Usa OAuth2 client_credentials para pegar um token.
        No projeto original os endpoints estao em /oauth/token.
        """
        token_url = f"{self.base_url}/oauth/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.scope,
        }
        resp = requests.post(token_url, json=data)
        if resp.status_code != 200:
            raise IntellisignAPIError(
                f"Erro ao obter token ({resp.status_code}): {resp.text}"
            )
        return resp.json()["access_token"]

    def _headers(self, access_token: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    # ---------- Envelopes ----------

    def create_envelope(self, access_token: str, name: str, subject: str, message: str) -> str:
        url = f"{self.base_url}/v1/envelopes"
        body: Dict[str, Any] = {
            "title": name,
            "subject": subject,
            "message": message,
        }
        resp = requests.post(url, json=body, headers=self._headers(access_token))
        if resp.status_code not in (200, 201):
            raise IntellisignAPIError(
                f"Erro ao criar envelope ({resp.status_code}): {resp.text}"
            )
        data = resp.json()
        envelope_id = data.get("id") or data.get("envelope_id")
        if not envelope_id:
            raise IntellisignAPIError("Envelope sem id na resposta")
        return envelope_id

    def add_document(
        self,
        access_token: str,
        envelope_id: str,
        file_path: Path,
        filename: str = "document.pdf",
    ) -> str:
        """
        Faz upload de um PDF para o envelope.
        """
        url = f"{self.base_url}/v1/envelopes/{envelope_id}/documents"
        with file_path.open("rb") as f:
            files = {
                "file": (filename, f, "application/pdf"),
            }
            data = {
                "name": filename,
                "stage": "original",
            }
            resp = requests.post(
                url, headers=self._headers(access_token), files=files, data=data
            )
        if resp.status_code not in (200, 201):
            raise IntellisignAPIError(
                f"Erro ao enviar documento ({resp.status_code}): {resp.text}"
            )
        data = resp.json()
        return data.get("id") or ""

    def add_recipient(
        self,
        access_token: str,
        envelope_id: str,
        name: str,
        email: str,
        signature_type: str = "simple",
        routing_order: Optional[int] = None,
    ):
        """
        Adiciona um destinatario (signatario) ao envelope.
        """
        url = f"{self.base_url}/v1/envelopes/{envelope_id}/recipients"
        body: Dict[str, Any] = {
            "type": "signer",
            "signature_type": signature_type,
            "addressees": [
                {
                    "via": "email",
                    "value": email,
                    "name": name,
                }
            ],
        }
        if routing_order is not None:
            body["routing_order"] = routing_order

        resp = requests.post(url, json=body, headers=self._headers(access_token))
        if resp.status_code not in (200, 201):
            raise IntellisignAPIError(
                f"Erro ao adicionar destinatario ({resp.status_code}): {resp.text}"
            )

    def send_envelope(self, access_token: str, envelope_id: str):
        url = f"{self.base_url}/v1/envelopes/{envelope_id}/send"
        resp = requests.post(url, headers=self._headers(access_token))
        # Alguns provedores auto-enviam quando adiciona destinatario; se der 404, trate conforme necessario.
        if resp.status_code not in (200, 201, 202, 204):
            raise IntellisignAPIError(
                f"Erro ao enviar envelope ({resp.status_code}): {resp.text}"
            )

    def get_envelope_status(self, access_token: str, envelope_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/v1/envelopes/{envelope_id}"
        resp = requests.get(url, headers=self._headers(access_token))
        if resp.status_code != 200:
            raise IntellisignAPIError(
                f"Erro ao consultar envelope ({resp.status_code}): {resp.text}"
            )
        return resp.json()

    def download_completed_document(
        self,
        access_token: str,
        envelope_id: str,
        destination: Path,
    ):
        """
        Baixa o PDF final do envelope usando as rotas ja testadas no projeto.
        """
        details = self.get_envelope_status(access_token, envelope_id)
        docs = details.get("documents") or []
        if not docs:
            raise IntellisignAPIError("Nenhum documento encontrado no envelope")
        doc = docs[0]
        links = doc.get("links") or {}
        download_link = links.get("download")
        if not download_link:
            doc_id = doc.get("id")
            if not doc_id:
                raise IntellisignAPIError("Documento sem id para download")
            download_link = f"{self.base_url}/v1/envelopes/{envelope_id}/documents/{doc_id}/download"

        resp = requests.get(
            download_link,
            headers=self._headers(access_token),
            stream=True,
        )
        if resp.status_code != 200:
            raise IntellisignAPIError(
                f"Erro ao baixar documento ({resp.status_code}): {resp.text}"
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)


# IMPORTANTE: confirme as rotas (/oauth/token, /v1/envelopes, /v1/envelopes/{id}/documents/{docId}/download) na doc oficial do Intellisign.
