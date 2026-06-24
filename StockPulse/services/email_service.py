from email.message import EmailMessage
import smtplib

from fastapi import HTTPException


def send_verification_email(recipient_email: str, otp_code: str, *, smtp_host: str, smtp_port: int, smtp_username: str, smtp_password: str, smtp_from: str, smtp_use_tls: bool, otp_expires_minutes: int):
    if not smtp_host:
        raise HTTPException(status_code=503, detail="Email service is not configured")

    message = EmailMessage()
    message["Subject"] = "StockPulse verification code"
    message["From"] = smtp_from or smtp_username
    message["To"] = recipient_email
    message.set_content(
        f"Your StockPulse verification code is {otp_code}. It expires in {otp_expires_minutes} minutes."
    )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as client:
        if smtp_use_tls:
            client.starttls()
        if smtp_username:
            client.login(smtp_username, smtp_password)
        client.send_message(message)