"""
AdLens email service — powered by SendGrid.

Sends two types of emails:
  1. Invitation email   — when platform admin invites a specific user to an org
  2. Welcome email      — when a new user signs in for the first time (Google OAuth)

Both emails are fully HTML, mobile + desktop responsive, and match the AdLens UI.
"""

import logging
from typing import Optional

from config import settings

_log = logging.getLogger(__name__)

# ── Brand colors (match index.css) ────────────────────────────────────────────
C = {
    "bg":       "#0B0F1A",
    "panel":    "#131929",
    "panel2":   "#1A2236",
    "wire":     "#1E2B42",
    "accent":   "#3B7EF6",
    "teal":     "#10D9A0",
    "chalk":    "#F0F4FF",
    "mist":     "#8B9DB8",
    "haze":     "#4A5770",
    "coral":    "#FF6B6B",
    "honey":    "#F59E0B",
}


# ── Base HTML template ─────────────────────────────────────────────────────────

def _base_template(preheader: str, body_html: str) -> str:
    """Wraps body_html in the AdLens branded email shell."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta http-equiv="X-UA-Compatible" content="IE=edge" />
  <title>AdLens</title>
  <!--[if mso]>
  <noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
  <![endif]-->
  <style>
    @media only screen and (max-width: 600px) {{
      .email-wrapper {{ width: 100% !important; padding: 0 !important; }}
      .email-body    {{ padding: 24px 20px !important; }}
      .btn-cta       {{ display: block !important; text-align: center !important; }}
      .stat-grid     {{ display: block !important; }}
      .stat-cell     {{ display: block !important; width: 100% !important; margin-bottom: 12px !important; }}
    }}
    body {{ margin: 0; padding: 0; background-color: {C['bg']}; }}
    * {{ box-sizing: border-box; }}
  </style>
</head>
<body style="margin:0;padding:0;background-color:{C['bg']};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">

  <!-- Preheader (hidden preview text) -->
  <span style="display:none;font-size:1px;color:{C['bg']};line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;">{preheader}</span>

  <!-- Outer wrapper -->
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{C['bg']};padding:40px 20px;">
    <tr>
      <td align="center">
        <table class="email-wrapper" role="presentation" width="600" cellpadding="0" cellspacing="0"
          style="max-width:600px;width:100%;background-color:{C['panel']};border-radius:16px;overflow:hidden;border:1px solid {C['wire']};">

          <!-- Header -->
          <tr>
            <td style="background:linear-gradient(135deg,{C['panel2']} 0%,{C['panel']} 100%);padding:32px 40px 28px;border-bottom:1px solid {C['wire']};">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td>
                    <!-- Logo mark -->
                    <table role="presentation" cellpadding="0" cellspacing="0">
                      <tr>
                        <td style="background:linear-gradient(135deg,{C['accent']},{C['teal']});border-radius:10px;padding:8px 10px;display:inline-block;">
                          <span style="font-size:14px;font-weight:800;color:#ffffff;letter-spacing:-0.5px;font-family:Georgia,serif;">Ad</span>
                        </td>
                        <td style="padding-left:10px;vertical-align:middle;">
                          <span style="font-size:20px;font-weight:800;color:{C['chalk']};letter-spacing:-0.5px;">AdLens</span>
                        </td>
                      </tr>
                    </table>
                    <p style="margin:8px 0 0;font-size:12px;color:{C['mist']};letter-spacing:0.04em;text-transform:uppercase;font-weight:500;">
                      Creative Intelligence Platform
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td class="email-body" style="padding:36px 40px;">
              {body_html}
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:24px 40px;border-top:1px solid {C['wire']};background-color:{C['panel2']};">
              <p style="margin:0 0 6px;font-size:12px;color:{C['haze']};text-align:center;line-height:1.6;">
                This email was sent by <strong style="color:{C['mist']};">AnalyticsLiv</strong> via AdLens.
              </p>
              <p style="margin:0;font-size:11px;color:{C['haze']};text-align:center;">
                &copy; 2026 AnalyticsLiv &nbsp;·&nbsp;
                <a href="{settings.APP_URL}" style="color:{C['accent']};text-decoration:none;">adlens.analyticsliv.com</a>
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>

</body>
</html>"""


# ── Email 1: Invitation ────────────────────────────────────────────────────────

def _invitation_body(org_name: str, inviter_name: str, role: str, app_url: str) -> str:
    role_label = "Administrator" if role == "admin" else "Member"
    role_color = C["honey"] if role == "admin" else C["teal"]

    return f"""
      <!-- Icon -->
      <div style="text-align:center;margin-bottom:28px;">
        <div style="display:inline-block;background:linear-gradient(135deg,{C['accent']}22,{C['teal']}22);border:1px solid {C['accent']}44;border-radius:50%;width:64px;height:64px;line-height:64px;text-align:center;font-size:28px;">
          ✉️
        </div>
      </div>

      <!-- Heading -->
      <h1 style="margin:0 0 12px;font-size:24px;font-weight:800;color:{C['chalk']};text-align:center;letter-spacing:-0.5px;line-height:1.3;">
        You've been invited to AdLens
      </h1>
      <p style="margin:0 0 28px;font-size:15px;color:{C['mist']};text-align:center;line-height:1.6;">
        <strong style="color:{C['chalk']};">{inviter_name}</strong> has invited you to join
        <strong style="color:{C['chalk']};">{org_name}</strong> on AdLens —
        the creative intelligence platform for performance marketing.
      </p>

      <!-- Org card -->
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{C['panel2']};border:1px solid {C['wire']};border-radius:12px;margin-bottom:28px;">
        <tr>
          <td style="padding:20px 24px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td>
                  <p style="margin:0 0 4px;font-size:11px;color:{C['haze']};text-transform:uppercase;letter-spacing:0.06em;font-weight:600;">Organization</p>
                  <p style="margin:0;font-size:17px;font-weight:700;color:{C['chalk']}">{org_name}</p>
                </td>
                <td align="right" style="vertical-align:top;">
                  <span style="display:inline-block;padding:4px 12px;border-radius:20px;background:{role_color}18;border:1px solid {role_color}40;font-size:12px;font-weight:700;color:{role_color};text-transform:uppercase;letter-spacing:0.04em;">{role_label}</span>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>

      <!-- What you get -->
      <p style="margin:0 0 14px;font-size:13px;font-weight:600;color:{C['mist']};text-transform:uppercase;letter-spacing:0.05em;">What you get access to</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:32px;">
        <tr>
          <td style="padding:0 0 10px;">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="width:28px;vertical-align:top;padding-top:2px;">
                  <span style="display:inline-block;width:20px;height:20px;background:{C['accent']}18;border:1px solid {C['accent']}40;border-radius:6px;text-align:center;line-height:20px;font-size:11px;">A</span>
                </td>
                <td style="padding-left:10px;">
                  <p style="margin:0;font-size:14px;font-weight:600;color:{C['chalk']};">ABCD Analyzer</p>
                  <p style="margin:2px 0 0;font-size:12px;color:{C['mist']};">Score your video ads against Google's ABCD framework</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:0 0 10px;">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="width:28px;vertical-align:top;padding-top:2px;">
                  <span style="display:inline-block;width:20px;height:20px;background:{C['teal']}18;border:1px solid {C['teal']}40;border-radius:6px;text-align:center;line-height:20px;font-size:11px;color:{C['teal']};">✦</span>
                </td>
                <td style="padding-left:10px;">
                  <p style="margin:0;font-size:14px;font-weight:600;color:{C['chalk']};">Creative Studio</p>
                  <p style="margin:2px 0 0;font-size:12px;color:{C['mist']};">AI-powered image enhancement for your ad creatives</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td>
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="width:28px;vertical-align:top;padding-top:2px;">
                  <span style="display:inline-block;width:20px;height:20px;background:{C['honey']}18;border:1px solid {C['honey']}40;border-radius:6px;text-align:center;line-height:20px;font-size:11px;color:{C['honey']};">↔</span>
                </td>
                <td style="padding-left:10px;">
                  <p style="margin:0;font-size:14px;font-weight:600;color:{C['chalk']};">Creative Resize</p>
                  <p style="margin:2px 0 0;font-size:12px;color:{C['mist']};">Resize creatives to any platform format instantly</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>

      <!-- CTA -->
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
        <tr>
          <td align="center">
            <a href="{app_url}" class="btn-cta"
              style="display:inline-block;padding:14px 40px;background:linear-gradient(135deg,{C['accent']},{C['teal']});border-radius:10px;font-size:15px;font-weight:700;color:#ffffff;text-decoration:none;letter-spacing:0.01em;">
              Accept Invitation &rarr;
            </a>
          </td>
        </tr>
      </table>

      <p style="margin:0;font-size:13px;color:{C['haze']};text-align:center;line-height:1.6;">
        Click the button and sign in with your Google account. Your invitation will be recognized automatically.
        <br/>This invitation expires in <strong style="color:{C['mist']};">72 hours</strong>.
      </p>
    """


# ── Email 2: Welcome ───────────────────────────────────────────────────────────

def _welcome_body(display_name: str, org_name: str, app_url: str, allowed_services: list) -> str:
    first_name = display_name.split()[0] if display_name else "there"

    svc_map = {
        "abcd_analyzer":    ("A", C["accent"],  "ABCD Analyzer",    "Score your video ads against Google's ABCD framework"),
        "creative_studio":  ("✦", C["teal"],    "Creative Studio",  "AI-powered image enhancement for your creatives"),
        "creative_resize":  ("↔", C["honey"],   "Creative Resize",  "Resize creatives to any platform format instantly"),
    }

    modules_html = ""
    for svc in allowed_services:
        if svc not in svc_map:
            continue
        icon, color, name, desc = svc_map[svc]
        modules_html += f"""
        <tr>
          <td style="padding:0 0 12px;">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="width:36px;vertical-align:top;padding-top:2px;">
                  <div style="width:28px;height:28px;background:{color}18;border:1px solid {color}40;border-radius:8px;text-align:center;line-height:28px;font-size:13px;color:{color};">{icon}</div>
                </td>
                <td style="padding-left:12px;">
                  <p style="margin:0;font-size:14px;font-weight:700;color:{C['chalk']};">{name}</p>
                  <p style="margin:2px 0 0;font-size:12px;color:{C['mist']};">{desc}</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""

    return f"""
      <!-- Welcome icon -->
      <div style="text-align:center;margin-bottom:28px;">
        <div style="display:inline-block;background:linear-gradient(135deg,{C['teal']}22,{C['accent']}22);border:1px solid {C['teal']}44;border-radius:50%;width:64px;height:64px;line-height:64px;text-align:center;font-size:28px;">
          🎉
        </div>
      </div>

      <!-- Heading -->
      <h1 style="margin:0 0 12px;font-size:24px;font-weight:800;color:{C['chalk']};text-align:center;letter-spacing:-0.5px;line-height:1.3;">
        Welcome to AdLens, {first_name}!
      </h1>
      <p style="margin:0 0 28px;font-size:15px;color:{C['mist']};text-align:center;line-height:1.6;">
        You've successfully joined <strong style="color:{C['chalk']};">{org_name}</strong>.
        Your account is active and ready to use.
      </p>

      <!-- Modules available -->
      <div style="background:{C['panel2']};border:1px solid {C['wire']};border-radius:12px;padding:20px 24px;margin-bottom:28px;">
        <p style="margin:0 0 16px;font-size:12px;font-weight:600;color:{C['haze']};text-transform:uppercase;letter-spacing:0.06em;">Your modules</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          {modules_html}
        </table>
      </div>

      <!-- Quick tips -->
      <div style="background:{C['accent']}0A;border:1px solid {C['accent']}25;border-radius:12px;padding:20px 24px;margin-bottom:32px;">
        <p style="margin:0 0 12px;font-size:13px;font-weight:700;color:{C['accent']};">Quick start tips</p>
        <p style="margin:0 0 8px;font-size:13px;color:{C['mist']};line-height:1.6;">
          <strong style="color:{C['chalk']};">1.</strong> Head to <strong style="color:{C['chalk']};">ABCD Analyzer</strong> — paste a YouTube URL or upload an MP4 to score your first ad creative.
        </p>
        <p style="margin:0 0 8px;font-size:13px;color:{C['mist']};line-height:1.6;">
          <strong style="color:{C['chalk']};">2.</strong> Use <strong style="color:{C['chalk']};">Creative Studio</strong> to enhance images with a simple prompt.
        </p>
        <p style="margin:0;font-size:13px;color:{C['mist']};line-height:1.6;">
          <strong style="color:{C['chalk']};">3.</strong> Check <strong style="color:{C['chalk']};">Creative Resize</strong> to export your best creatives to any platform size in one click.
        </p>
      </div>

      <!-- CTA -->
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;">
        <tr>
          <td align="center">
            <a href="{app_url}" class="btn-cta"
              style="display:inline-block;padding:14px 40px;background:linear-gradient(135deg,{C['accent']},{C['teal']});border-radius:10px;font-size:15px;font-weight:700;color:#ffffff;text-decoration:none;letter-spacing:0.01em;">
              Open AdLens &rarr;
            </a>
          </td>
        </tr>
      </table>

      <p style="margin:0;font-size:13px;color:{C['haze']};text-align:center;">
        Questions? Reply to this email or contact <a href="mailto:atul.verma@analyticsliv.com" style="color:{C['accent']};text-decoration:none;">atul.verma@analyticsliv.com</a>
      </p>
    """


# ── SendGrid sender ────────────────────────────────────────────────────────────

def _send(to_email: str, to_name: str, subject: str, html: str) -> bool:
    """
    Send a single email via SendGrid.
    Returns True on success, False on failure (logs the error).
    Silently skips if SENDGRID_API_KEY is not configured.
    """
    if not settings.SENDGRID_API_KEY:
        _log.warning("SENDGRID_API_KEY not set — skipping email to %s", to_email)
        return False

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, Email, To, Content

        message = Mail(
            from_email=Email(settings.EMAIL_FROM, settings.EMAIL_FROM_NAME),
            to_emails=To(to_email, to_name or to_email),
            subject=subject,
            html_content=Content("text/html", html),
        )
        sg = SendGridAPIClient(settings.SENDGRID_API_KEY)
        response = sg.send(message)
        _log.info("Email sent to %s (status %s)", to_email, response.status_code)
        return True

    except Exception as exc:
        _log.error("Failed to send email to %s: %s", to_email, exc)
        return False


# ── Public API ─────────────────────────────────────────────────────────────────

def send_invitation_email(
    to_email: str,
    org_name: str,
    inviter_name: str,
    role: str = "member",
) -> bool:
    """Send an invitation email to a user being added to an org."""
    body = _invitation_body(
        org_name=org_name,
        inviter_name=inviter_name,
        role=role,
        app_url=settings.APP_URL,
    )
    html = _base_template(
        preheader=f"You've been invited to join {org_name} on AdLens",
        body_html=body,
    )
    return _send(
        to_email=to_email,
        to_name=to_email.split("@")[0].replace(".", " ").title(),
        subject=f"You're invited to {org_name} on AdLens",
        html=html,
    )


def send_welcome_email(
    to_email: str,
    display_name: str,
    org_name: str,
    allowed_services: Optional[list] = None,
) -> bool:
    """Send a welcome email to a user who just signed in for the first time."""
    body = _welcome_body(
        display_name=display_name or to_email.split("@")[0],
        org_name=org_name,
        app_url=settings.APP_URL,
        allowed_services=allowed_services or ["abcd_analyzer"],
    )
    html = _base_template(
        preheader=f"Welcome to AdLens — your account is ready",
        body_html=body,
    )
    first_name = (display_name or "").split()[0] or "there"
    return _send(
        to_email=to_email,
        to_name=display_name or to_email,
        subject=f"Welcome to AdLens, {first_name}! 🎉",
        html=html,
    )
