"""Тонкие glyph-полосы с сохранением штатных drag/click метаданных Textual."""
from rich.segment import Segment, Segments
from rich.style import Style
from textual.scrollbar import ScrollBarRender, ScrollBar


class ThinScrollBarRender(ScrollBarRender):
    @classmethod
    def render_bar(cls, **kwargs):
        rendered=super().render_bar(**kwargs)
        vertical=kwargs.get('vertical',True)
        segments=[]
        for segment in rendered.segments:
            style=segment.style
            if style and style.meta.get('@mouse.down')=='grab' and segment.text!='\n':
                style=style+Style(color=kwargs['bar_color'],bgcolor=kwargs['back_color'],reverse=False)
                segment=Segment(('▏' if vertical else '▁')*len(segment.text),style)
            segments.append(segment)
        return Segments(segments,new_lines=rendered.new_lines)


def use_thin_scrollbars():
    # Публичный extension point Textual; поведение самого scrollbar не меняется.
    ScrollBar.renderer=ThinScrollBarRender
