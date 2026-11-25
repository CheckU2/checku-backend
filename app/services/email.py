import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from app.config import get_settings

def send_email(to_email: str, subject: str, body: str, *, html: str | None = None) -> None:
    s=get_settings()
    if not all([s.smtp_host,s.smtp_from_email]):
        raise RuntimeError("SMTP_HOST y SMTP_FROM_EMAIL deben configurarse para enviar correos reales")
    msg=EmailMessage()
    msg["Subject"]=subject
    msg["From"] = formataddr((s.smtp_from_name, s.smtp_from_email))
    msg["To"] = to_email
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(s.smtp_host,s.smtp_port,timeout=10) as server:
        if s.smtp_use_tls: server.starttls()
        if s.smtp_username: server.login(s.smtp_username,s.smtp_password)
        server.send_message(msg)


def send_password_reset_email(to_email: str, reset_token: str) -> None:
    s = get_settings()
    send_email(
        to_email,
        "Recuperación de contraseña de CheckU",
        f"Recibimos una solicitud para restablecer tu contraseña de CheckU. "
        f"Usa este enlace dentro de {s.password_reset_expire_minutes} minutos:\n\n"
        f"{s.password_reset_base_url}?token={reset_token}\n\n"
        "Si no solicitaste este cambio, ignora este mensaje.",
    )
