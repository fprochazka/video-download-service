"""Entrypoint: `python main.py`, or `uvicorn main:app`"""

from video_download.web import app

__all__ = ['app']

if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host='0.0.0.0', port=8000)
