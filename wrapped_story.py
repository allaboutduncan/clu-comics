"""The Wrapped story slides: streaks, time, creators, characters, the reading
clock, a reading personality and the goal.

``wrapped.py`` holds the original slides (totals, series, cover grids). The
slides here are the ones that tell the rest of the story, and they differ in
one way that matters: **every one is drawn from a single dict**,
``models.reading_insights.period_story``. No slide runs its own query, so two
slides cannot disagree about a number, and the registry's ``has_data`` checks
read the same dict to decide whether a slide exists at all.

Each generator has the signature the registry calls (``(year, theme)`` for a
yearly slide, ``(year, month, theme)`` for a monthly one) and returns PNG
bytes at 1080x1920. Colours, fonts and backgrounds come from ``wrapped``'s
three style-aware helpers, so every style draws these without any code here
knowing which style is active.
"""
import io
import math
from datetime import date

from PIL import ImageDraw

import wrapped
from wrapped import IMAGE_HEIGHT, IMAGE_WIDTH, MONTH_NAMES, hex_to_rgb
from models.reading_insights import period_story

YEARLY = "yearly"
MONTHLY = "monthly"

MARGIN = 90
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAY_SHORT = ["M", "T", "W", "T", "F", "S", "S"]


# --- small helpers -----------------------------------------------------------

def format_duration(seconds):
    """"42h 15m", "38m" -- how the app writes reading time everywhere."""
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60:,}h {minutes % 60}m"


def format_hour(hour):
    hour %= 24
    return f"{12 if hour % 12 == 0 else hour % 12}{'am' if hour < 12 else 'pm'}"


def format_day(iso, with_year=False):
    """"Jun 3" from an ISO date (no leading zero, on every platform)."""
    if not iso:
        return ""
    day = date.fromisoformat(iso)
    text = f"{day.strftime('%b')} {day.day}"
    return f"{text}, {day.year}" if with_year else text


def plural(count, word):
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _fit_font(draw, text, max_width, size, bold=True, minimum=28):
    """The largest font up to *size* at which *text* fits *max_width*."""
    while size > minimum:
        font = wrapped.get_font(size, bold=bold)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
        size -= 4
    return wrapped.get_font(minimum, bold=bold)


def _ellipsize(draw, text, font, max_width):
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text
    while len(text) > 1 and draw.textbbox((0, 0), text + "…", font=font)[2] > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


class Canvas:
    """A slide being drawn: the image, its palette as RGB, and where the
    content may start. Monthly slides sit on the monthly canvas (the title is
    already there); yearly slides start nearer the top."""

    def __init__(self, scope, year, month, theme):
        self.scope, self.year, self.month = scope, year, month
        colors = wrapped.get_theme_colors(theme)
        if scope == MONTHLY:
            self.img, bg, text, muted, accent = wrapped._monthly_canvas(theme)
            self.top = 230
        else:
            self.img = wrapped.create_base_image(colors).convert("RGB")
            bg, text, muted = (hex_to_rgb(colors[k]) for k in ("bg", "text", "text_muted"))
            accent = hex_to_rgb(colors["primary"])
            self.top = 150
        self.bg, self.text, self.muted, self.accent = bg, text, muted, accent
        self.second = hex_to_rgb(colors["info"]) if scope == YEARLY else muted
        self.draw = ImageDraw.Draw(self.img)

    def centered(self, text, y, size, color, bold=True, max_width=IMAGE_WIDTH - 2 * MARGIN):
        font = _fit_font(self.draw, text, max_width, size, bold=bold)
        box = self.draw.textbbox((0, 0), text, font=font)
        self.draw.text(((IMAGE_WIDTH - box[2]) // 2, y), text, font=font, fill=color)
        return y + (box[3] - box[1]) + int(size * 0.35)

    def title(self, text):
        y = self.centered(text.upper(), self.top, 64, self.accent)
        self.draw.line([(MARGIN, y + 10), (IMAGE_WIDTH - MARGIN, y + 10)], fill=self.muted, width=3)
        return y + 60

    def footer(self):
        if self.scope == MONTHLY:
            label = f"{MONTH_NAMES[self.month - 1].upper()} {self.year}"
            tagline = "Monthly Reading Recap • Comic Library Utilities"
        else:
            label = f"{self.year} WRAPPED"
            tagline = "Your Year in Comics • Comic Library Utilities"
        self.centered(label, IMAGE_HEIGHT - 200, 72, self.accent)
        self.centered(tagline, IMAGE_HEIGHT - 100, 28, self.muted, bold=False)

    def png(self):
        self.footer()
        buffer = io.BytesIO()
        self.img.save(buffer, format="PNG")
        return buffer.getvalue()


def _canvas_and_story(scope, year, month, theme):
    return Canvas(scope, year, month, theme), period_story(scope, year, month)


# --- stat blocks: a big value, a label, a line of detail ----------------------

def _stat_blocks(canvas, y, blocks, value_size=150):
    """Draw ``[(value, label, detail)]`` down the slide, evenly spaced in the
    room that is left."""
    room = IMAGE_HEIGHT - 260 - y
    step = room // max(len(blocks), 1)
    for index, (value, label, detail) in enumerate(blocks):
        top = y + index * step
        color = canvas.accent if index % 2 == 0 else canvas.second
        bottom = canvas.centered(value, top, min(value_size, int(step * 0.42)), color)
        bottom = canvas.centered(label.upper(), bottom, 44, canvas.text)
        if detail:
            canvas.centered(detail, bottom, 34, canvas.muted, bold=False)


def _streak_blocks(story):
    streak = story["longest_streak"]
    if streak > 1 and story["longest_streak_start"]:
        when = f"{format_day(story['longest_streak_start'])} – {format_day(story['longest_streak_end'])}"
    else:
        when = None
    blocks = [
        (plural(streak, "day"), "Longest streak", when),
        (f"{story['active_days']:,}", "Days you read",
         f"out of {plural(story['period_days'], 'day')}"),
    ]
    if story["busiest_day"]:
        blocks.append((format_day(story["busiest_day"]), "Biggest day",
                       plural(story["busiest_day_issues"], "issue")))
    return blocks


def _time_blocks(story):
    blocks = []
    if story["seconds"] > 0:
        per_issue = story["seconds"] / max(story["issues"], 1)
        blocks.append((format_duration(story["seconds"]), "Time reading",
                       f"about {format_duration(per_issue)} an issue"))
    if story["pages"] > 0:
        blocks.append((f"{story['pages']:,}", "Pages turned",
                       f"across {plural(story['issues'], 'issue')}"))
    blocks.append((f"{story['series']:,}", "Series" if story["series"] != 1 else "Series read",
                   None))
    return blocks


def has_time(story):
    return story["issues"] > 0 and (story["seconds"] > 0 or story["pages"] > 0)


def generate_streaks_slide(year, theme):
    canvas, story = _canvas_and_story(YEARLY, year, None, theme)
    _stat_blocks(canvas, canvas.title("Your Streaks"), _streak_blocks(story))
    return canvas.png()


def generate_time_pages_slide(year, theme):
    canvas, story = _canvas_and_story(YEARLY, year, None, theme)
    _stat_blocks(canvas, canvas.title("Time Well Spent"), _time_blocks(story))
    return canvas.png()


def generate_monthly_numbers_slide(year, month, theme):
    canvas, story = _canvas_and_story(MONTHLY, year, month, theme)
    blocks = _time_blocks(story)[:2] + _streak_blocks(story)[:2]
    _stat_blocks(canvas, canvas.title("The Month in Numbers"), blocks, value_size=120)
    return canvas.png()


# --- ranked lists --------------------------------------------------------------

def _ranked_section(canvas, y, heading, rows, row_height=74):
    """A heading and up to five ``{name, count}`` rows, each with a bar sized
    against the first. Type scales with *row_height*."""
    draw = canvas.draw
    y = canvas.centered(heading.upper(), y, 40, canvas.muted)
    y += 6
    name_size = int(row_height * 0.5)
    name_font = wrapped.get_font(name_size, bold=True)
    count_font = wrapped.get_font(int(name_size * 0.9))
    width = IMAGE_WIDTH - 2 * MARGIN
    top_count = max((row["count"] for row in rows), default=1)
    for row in rows:
        count_text = f"{row['count']:,}"
        count_width = draw.textbbox((0, 0), count_text, font=count_font)[2]
        name = _ellipsize(draw, row["name"], name_font, width - count_width - 30)
        draw.text((MARGIN, y), name, font=name_font, fill=canvas.text)
        draw.text((IMAGE_WIDTH - MARGIN - count_width, y + 3), count_text,
                  font=count_font, fill=canvas.muted)
        bar_y = y + int(name_size * 1.35)
        draw.rectangle([MARGIN, bar_y, IMAGE_WIDTH - MARGIN, bar_y + 6], fill=canvas.muted)
        filled = max(8, int(width * row["count"] / top_count))
        draw.rectangle([MARGIN, bar_y, MARGIN + filled, bar_y + 6], fill=canvas.accent)
        y += row_height
    return y + 40


def _ranked_slide(canvas, title, sections):
    y = canvas.title(title)
    sections = [(heading, rows) for heading, rows in sections if rows]
    # Rows as tall as the slide allows, so three names do not huddle at the
    # top of an otherwise empty slide and ten still fit.
    total_rows = sum(len(rows) for _, rows in sections) or 1
    room = IMAGE_HEIGHT - 280 - y - 110 * len(sections)
    row_height = max(74, min(124, room // total_rows))
    for heading, rows in sections:
        y = _ranked_section(canvas, y, heading, rows, row_height)
    return canvas.png()


def has_creators(story):
    return bool(story["credits"]["writers"] or story["credits"]["artists"])


def has_characters(story):
    return bool(story["credits"]["characters"])


def generate_creators_slide(year, theme):
    canvas, story = _canvas_and_story(YEARLY, year, None, theme)
    return _ranked_slide(canvas, "Your Creators", [
        ("Writers", story["credits"]["writers"]), ("Artists", story["credits"]["artists"])])


def generate_characters_slide(year, theme):
    canvas, story = _canvas_and_story(YEARLY, year, None, theme)
    return _ranked_slide(canvas, "Your Universe", [
        ("Characters", story["credits"]["characters"]),
        ("Publishers", story["credits"]["publishers"])])


def generate_monthly_creators_slide(year, month, theme):
    canvas, story = _canvas_and_story(MONTHLY, year, month, theme)
    return _ranked_slide(canvas, "Who You Read", [
        ("Writers", story["credits"]["writers"][:4]),
        ("Artists", story["credits"]["artists"][:4]),
        ("Characters", story["credits"]["characters"][:4])])


# --- reading clock --------------------------------------------------------------

def has_clock(story):
    return story["clock"]["source"] != "none"


def _draw_clock(canvas, y, clock):
    """A 24-hour clock face, midnight at the top, one wedge an hour, reaching
    out by how much reading fell in it. Then the week as seven bars."""
    draw = canvas.draw
    hours = clock["hours"]
    radius = 360
    cx, cy = IMAGE_WIDTH // 2, y + radius + 90
    peak = max(hours) or 1

    for ring in (0.33, 0.66, 1.0):
        r = radius * ring
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=canvas.muted, width=2)
    for hour, seconds in enumerate(hours):
        if seconds <= 0:
            continue
        # Area, not radius, in proportion to the time: a wedge twice as long
        # would otherwise look four times as busy.
        r = max(radius * math.sqrt(seconds / peak), 14)
        start = hour * 15 - 90 + 1
        color = canvas.accent if hour == clock["peak_hour"] else canvas.second
        draw.pieslice([cx - r, cy - r, cx + r, cy + r], start, start + 13, fill=color)
    draw.ellipse([cx - 10, cy - 10, cx + 10, cy + 10], fill=canvas.text)

    label_font = wrapped.get_font(32, bold=True)
    for hour in (0, 6, 12, 18):
        angle = math.radians(hour * 15 - 90)
        lx, ly = cx + (radius + 46) * math.cos(angle), cy + (radius + 46) * math.sin(angle)
        text = format_hour(hour)
        box = draw.textbbox((0, 0), text, font=label_font)
        draw.text((lx - box[2] / 2, ly - box[3] / 2 - 4), text, font=label_font, fill=canvas.muted)

    y = cy + radius + 100
    y = canvas.centered(f"Most reading around {format_hour(clock['peak_hour'])}", y, 48, canvas.text)

    # The week.
    weekdays = clock["weekdays"]
    top_day = max(weekdays) or 1
    bar_width, gap, max_height = 84, 28, 190
    left = (IMAGE_WIDTH - (7 * bar_width + 6 * gap)) // 2
    base = y + 40 + max_height
    day_font = wrapped.get_font(30, bold=True)
    for index, seconds in enumerate(weekdays):
        x = left + index * (bar_width + gap)
        height = max(6, int(max_height * seconds / top_day))
        color = canvas.accent if index == clock["peak_weekday"] else canvas.second
        draw.rectangle([x, base - height, x + bar_width, base], fill=color)
        box = draw.textbbox((0, 0), WEEKDAY_SHORT[index], font=day_font)
        draw.text((x + (bar_width - box[2]) / 2, base + 12), WEEKDAY_SHORT[index],
                  font=day_font, fill=canvas.muted)
    canvas.centered(f"{WEEKDAY_NAMES[clock['peak_weekday']]}s are your day", base + 70, 40,
                    canvas.muted, bold=False)


def _clock_slide(scope, year, month, theme):
    canvas, story = _canvas_and_story(scope, year, month, theme)
    _draw_clock(canvas, canvas.title("Your Reading Clock"), story["clock"])
    return canvas.png()


def generate_clock_slide(year, theme):
    return _clock_slide(YEARLY, year, None, theme)


def generate_monthly_clock_slide(year, month, theme):
    return _clock_slide(MONTHLY, year, month, theme)


# --- personality ----------------------------------------------------------------

def has_personality(story):
    return story["issues"] > 0


def generate_personality_slide(year, theme):
    canvas, story = _canvas_and_story(YEARLY, year, None, theme)
    personality = story["personality"]
    y = canvas.title("Your Reading Personality")
    y = canvas.centered("You are", y + 300, 52, canvas.muted, bold=False)
    for line in _two_lines(personality["label"]):
        y = canvas.centered(line.upper(), y + 10, 130, canvas.accent)
    y += 90
    y = wrapped.draw_centered_text(canvas.draw, personality["blurb"], y,
                                   wrapped.get_font(52), canvas.text,
                                   max_width=IMAGE_WIDTH - 2 * MARGIN)
    canvas.draw.line([(IMAGE_WIDTH // 2 - 90, y + 50), (IMAGE_WIDTH // 2 + 90, y + 50)],
                     fill=canvas.muted, width=3)
    canvas.centered(personality["evidence"], y + 100, 46, canvas.second)
    return canvas.png()


def _two_lines(label):
    """Split "The Daily Ritualist" into "The Daily" / "Ritualist" so the name
    can be set large; a single word stays on one line."""
    words = label.split()
    if len(words) < 2:
        return [label]
    return [" ".join(words[:-1]), words[-1]]


# --- goal -----------------------------------------------------------------------

def has_goal(story):
    return bool(story.get("goal"))


def generate_goal_slide(year, theme):
    canvas, story = _canvas_and_story(YEARLY, year, None, theme)
    goal = story["goal"]
    y = canvas.title("Your Reading Goal")
    draw = canvas.draw

    radius, thickness = 340, 52
    cx, cy = IMAGE_WIDTH // 2, y + radius + 150
    box = [cx - radius, cy - radius, cx + radius, cy + radius]
    draw.arc(box, 0, 360, fill=canvas.muted, width=thickness)
    fraction = min(goal["read"] / goal["goal"], 1)
    if fraction > 0:
        draw.arc(box, -90, -90 + 360 * fraction, fill=canvas.accent, width=thickness)

    value = f"{goal['read']:,}"
    font = _fit_font(draw, value, 2 * radius - 2 * thickness - 60, 190)
    value_box = draw.textbbox((0, 0), value, font=font)
    draw.text((cx - value_box[2] / 2, cy - (value_box[3] + value_box[1]) / 2 - 30), value,
              font=font, fill=canvas.text)
    caption = f"of {goal['goal']:,}"
    caption_font = wrapped.get_font(48)
    caption_box = draw.textbbox((0, 0), caption, font=caption_font)
    draw.text((cx - caption_box[2] / 2, cy + 90), caption, font=caption_font, fill=canvas.muted)

    y = cy + radius + 110
    if goal["reached"]:
        extra = goal["read"] - goal["goal"]
        y = canvas.centered("GOAL REACHED", y, 96, canvas.accent)
        canvas.centered(f"{plural(extra, 'issue')} past it" if extra else "Right on the number",
                        y + 10, 44, canvas.text, bold=False)
    else:
        y = canvas.centered(f"{round(goal['pct'])}% OF THE WAY", y, 88, canvas.accent)
        if goal["complete"]:
            detail = f"{plural(goal['goal'] - goal['read'], 'issue')} short"
        else:
            detail = f"On course for {goal['projected']:,} by the end of the year"
        canvas.centered(detail, y + 10, 44, canvas.text, bold=False)
    return canvas.png()
