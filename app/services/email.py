import smtplib
from email.message import EmailMessage
from app.config import get_settings

def send_password_reset_email(to_email: str, reset_token: str) -> None:
    s=get_settings()
    if not all([s.smtp_host,s.smtp_from_email]):
        raise RuntimeError("SMTP_HOST y SMTP_FROM_EMAIL deben configurarse para enviar correos reales")
    msg=EmailMessage()
    msg["Subject"]="Recuperación de contraseña de CheckU"
    msg["From"]=s.smtp_from_email; msg["To"]=to_email
    msg.set_content(f"Recibimos una solicitud para restablecer tu contraseña de CheckU. Usa este enlace dentro de {s.password_reset_expire_minutes} minutos:\n\n{s.password_reset_base_url}?token={reset_token}\n\nSi no solicitaste este cambio, ignora este mensaje.")
    with smtplib.SMTP(s.smtp_host,s.smtp_port,timeout=10) as server:
        if s.smtp_use_tls: server.starttls()
        if s.smtp_username: server.login(s.smtp_username,s.smtp_password)
        server.send_message(msg)
