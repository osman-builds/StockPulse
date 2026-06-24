from pathlib import Path

from fastapi.templating import Jinja2Templates


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def render_template(template_name: str, **context) -> str:
    return templates.get_template(template_name).render(**context)