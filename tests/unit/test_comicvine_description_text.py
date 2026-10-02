"""ComicVine descriptions are HTML; ComicInfo <Summary> is plain text.

The API and the local dump both store descriptions as HTML, and both reach
ComicInfo through models.comicvine.map_to_comicinfo. Writing them through
verbatim showed ``<p>``/``<br />`` as literal text in the CBZ info modal.
"""
from models.comicvine import description_to_text, map_to_comicinfo


class TestDescriptionToText:
    def test_reported_local_db_description(self):
        raw = ("<p>Katchoo's ongoing dislike of alarm-clocks continues as she finds "
               "Francine somewhat unbalanced still from her blow to the head. <br /> <br />")
        assert description_to_text(raw) == (
            "Katchoo's ongoing dislike of alarm-clocks continues as she finds "
            "Francine somewhat unbalanced still from her blow to the head."
        )

    def test_paragraphs_become_line_breaks(self):
        raw = "<p>First part.</p><p>Second &amp; last.</p>"
        assert description_to_text(raw) == "First part.\n\nSecond & last."

    def test_inline_tags_keep_their_text(self):
        raw = '<p>Starring <a href="/x/4005-1/">Katchoo</a> and <em>Francine</em>.</p>'
        assert description_to_text(raw) == "Starring Katchoo and Francine."

    def test_cover_table_and_heading_are_dropped(self):
        raw = ("<p>The story.</p><h4>List of covers and their creators:</h4>"
               "<table><tr><th>Cover</th><th>Name</th></tr>"
               "<tr><td>Reg</td><td>Terry Moore</td></tr></table>")
        assert description_to_text(raw) == "The story."

    def test_plain_text_is_unchanged(self):
        assert description_to_text("Batman saves a plane.") == "Batman saves a plane."

    def test_less_than_without_markup_survives(self):
        assert description_to_text("Odds are < 5%.") == "Odds are < 5%."

    def test_empty_results_are_none(self):
        assert description_to_text(None) is None
        assert description_to_text("") is None
        assert description_to_text("<p> <br /> </p>") is None


class TestMapToComicInfo:
    def test_summary_is_plain_text(self):
        result = map_to_comicinfo({
            "volume_name": "Strangers in Paradise",
            "issue_number": "2",
            "description": "<p>Katchoo wakes up.<br /><br />Francine is &quot;fine&quot;.</p>",
        })
        assert result["Summary"] == 'Katchoo wakes up.\n\nFrancine is "fine".'
