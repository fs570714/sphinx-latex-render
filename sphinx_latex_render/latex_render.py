"""
Sphinx extension: latex_render

Compiles fenced ``latex`` code blocks to SVG images via pdflatex + pdf2svg.
Uses the **same syntax** as Markdown Preview Enhanced's code chunks, so a
single source file works in both MPE preview and Sphinx builds.

MPE syntax (unchanged)::

    ```latex {cmd=true latex_zoom=2}
    \\documentclass{standalone}
    \\usepackage{circuitikz}
    \\begin{document}
    \\begin{circuitikz}
      \\draw (0,0) to[battery1, l=$V$] (0,3)
            to[R=$R_1$] (3,3) ...
    \\end{circuitikz}
    \\end{document}
    ```

Supported MPE attributes (same keys work in both):

    latex_zoom      Image scale factor (e.g. 1, 2, 0.5)
    latex_engine    pdflatex, xelatex, or lualatex
    latex_width     Image width (e.g. '300px')
    latex_height    Image height (e.g. '200px')

Configuration in conf.py::

    extensions = ['sphinx_latex_render.latex_render']

    # Optional (shown with defaults):
    latex_render_engine = 'pdflatex'
    latex_render_preamble = ''
"""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from docutils import nodes
from sphinx.transforms.post_transforms import SphinxPostTransform
from sphinx.util import logging

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r'^(`{3,})\s*latex\s+\{([^}]*)\}\s*$', re.MULTILINE)
_ATTR_TAG = '% _latex_render_attrs:'
_CARRIED_KEYS = ('latex_zoom', 'latex_width', 'latex_height', 'latex_engine')
_UNIT_RE = re.compile(r'^([\d.]+)\s*(pt|px|em|cm|mm|in)?$')
_PX_FACTORS = {'px': 1, 'pt': 96 / 72, 'in': 96, 'cm': 96 / 2.54,
               'mm': 96 / 25.4, 'em': 16}


def _parse_kv(text):
    """Parse 'k1=v1 k2=v2 ...' into a dict."""
    return dict(p.split('=', 1) for p in text.split() if '=' in p)


def _to_px(val):
    """Convert an SVG dimension string (e.g. '131.18pt') to pixels."""
    m = _UNIT_RE.match(val.strip())
    if not m:
        return None
    return float(m.group(1)) * _PX_FACTORS.get(m.group(2) or 'px', 1)


def _get_svg_size(svg_path):
    """Return (width_px, height_px) from an SVG file's root element."""
    root = ET.parse(svg_path).getroot()
    return _to_px(root.get('width', '')), _to_px(root.get('height', ''))


def _on_source_read(app, docname, source):
    """Inject MPE fence attributes into the code block as a LaTeX comment.
    MyST strips the ``{...}`` from the info string, so this is needed."""

    def _inject(match):
        backticks, attr_str = match.group(1), match.group(2)
        carried = {k: v for k, v in _parse_kv(attr_str).items()
                   if k in _CARRIED_KEYS}
        if not carried:
            return f'{backticks}latex'
        tag = ' '.join(f'{k}={v}' for k, v in carried.items())
        return f'{backticks}latex\n{_ATTR_TAG} {tag}'

    source[0] = _FENCE_RE.sub(_inject, source[0])


def _extract_attrs(tex_body):
    """Extract and strip injected attribute comments from code body."""
    attrs, clean = {}, []
    for line in tex_body.split('\n'):
        if line.startswith(_ATTR_TAG):
            attrs.update(_parse_kv(line[len(_ATTR_TAG):]))
        else:
            clean.append(line)
    return '\n'.join(clean), attrs


class LaTeXCodeBlockTransform(SphinxPostTransform):
    """Replaces ``literal_block`` nodes with language='latex' by compiled images."""

    default_priority = 400

    def run(self):
        for node in self.document.findall(nodes.literal_block):
            if node.get('language') != 'latex':
                continue
            tex_body, attrs = _extract_attrs(node.astext())
            if '\\begin{' not in tex_body and '\\draw' not in tex_body:
                continue
            try:
                node.replace_self(self._render(tex_body, attrs))
            except LaTeXRenderError as exc:
                logger.warning('latex_render: %s', exc, location=node)

    def _render(self, tex_body, attrs):
        """Compile tex source to an image node."""
        app, env = self.app, self.env
        engine = (attrs.get('latex_engine')
                  or app.config.latex_render_engine or 'pdflatex')
        preamble = app.config.latex_render_preamble or ''
        zoom = attrs.get('latex_zoom')
        width = attrs.get('latex_width')
        height = attrs.get('latex_height')

        # Build full .tex source
        if '\\documentclass' in tex_body:
            tex_source = tex_body
            if preamble:
                tex_source = tex_source.replace(
                    '\\begin{document}',
                    f'{preamble}\n\\begin{{document}}', 1)
        else:
            tex_source = (f'\\documentclass{{standalone}}\n{preamble}\n'
                          f'\\begin{{document}}\n{tex_body}\n\\end{{document}}\n')

        # Output paths (cached by content hash)
        checksum = hashlib.md5(tex_source.encode()).hexdigest()
        out_dir = os.path.join(app.outdir, '_latex_render')
        os.makedirs(out_dir, exist_ok=True)
        svg_path = os.path.join(out_dir, f'{checksum}.svg')
        pdf_path = os.path.join(out_dir, f'{checksum}.pdf')

        if not os.path.isfile(svg_path) or not os.path.isfile(pdf_path):
            _compile_latex(tex_source, engine, svg_path, pdf_path)

        # Build image node
        is_latex = app.builder.format == 'latex'
        uri = os.path.abspath(pdf_path) if is_latex else \
              '_latex_render/' + os.path.basename(svg_path)

        img = nodes.image(uri=uri, alt='LaTeX rendered image',
                          candidates={'*': uri})

        if width:
            img['width'] = width
        if height:
            img['height'] = height

        # Apply zoom: scale for LaTeX, explicit px dimensions for HTML
        if zoom:
            z = float(zoom)
            if is_latex:
                img['scale'] = int(z * 100)
            else:
                sw, sh = _get_svg_size(svg_path)
                if sw and sh:
                    img['width'] = f'{sw * z:.0f}px'
                    img['height'] = f'{sh * z:.0f}px'

        return img


def _compile_latex(tex_source, engine, svg_out_path, pdf_out_path):
    """Compile tex_source to SVG and PDF via latex engine + pdf2svg."""
    with tempfile.TemporaryDirectory(prefix='sphinx_latex_render_') as tmpdir:
        tex_path = os.path.join(tmpdir, 'input.tex')
        pdf_path = os.path.join(tmpdir, 'input.pdf')

        with open(tex_path, 'w', encoding='utf-8') as f:
            f.write(tex_source)

        result = subprocess.run(
            [engine, '-interaction=nonstopmode', '-halt-on-error', 'input.tex'],
            cwd=tmpdir, capture_output=True, text=True, timeout=60)

        if result.returncode != 0 or not os.path.isfile(pdf_path):
            lines = (result.stdout or '').splitlines()
            errors = '\n'.join(l for l in lines if l.startswith('!'))[:500]
            raise LaTeXRenderError(
                f'{engine} failed (exit {result.returncode}):\n'
                f'{errors or result.stderr or "Unknown error"}')

        shutil.copy2(pdf_path, pdf_out_path)

        if not shutil.which('pdf2svg'):
            raise LaTeXRenderError('pdf2svg not found in PATH.')

        result = subprocess.run(
            ['pdf2svg', pdf_path, svg_out_path],
            capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            raise LaTeXRenderError(
                f'pdf2svg failed (exit {result.returncode}): {result.stderr}')


class LaTeXRenderError(Exception):
    pass


def setup(app):
    app.add_config_value('latex_render_engine', 'pdflatex', 'env')
    app.add_config_value('latex_render_preamble', '', 'env')
    app.connect('source-read', _on_source_read)
    app.add_post_transform(LaTeXCodeBlockTransform)
    return {'version': '0.1.0',
            'parallel_read_safe': True,
            'parallel_write_safe': True}
