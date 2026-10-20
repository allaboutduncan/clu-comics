/**
 * Rename and move pattern previews: the Settings page (File Processing) and
 * the Guided Setup's Folder Monitoring step.
 *
 * There used to be one copy, inside config.html, which read the character map
 * and the spaces option straight out of that page's form. The wizard has no
 * such form -- it previews against the *saved* cleanup -- so the cleanup is a
 * parameter here, in the shape CLU.applyFilenameCleanup takes:
 * {char_map, spaces_enabled, spaces_mode, spaces_replacement}.
 */
(function (global) {
  'use strict';

  const CLU = (global.CLU = global.CLU || {});

  CLU.RENAME_SAMPLE_VALUES = {
    publisher: 'Marvel',
    series_name: 'Spider-Man 2099',
    volume_number: 'v2',
    volume_year: '1992',
    year: '1992',
    issue_year: '1992',
    issue_month_M: 'June',
    issue_month_m: '06',
    issue_number: '044',
    issue_title: 'The Last Dance'
  };

  CLU.MOVE_SAMPLE_VALUES = {
    publisher: 'Marvel',
    series_name: 'Spider-Man 2099',
    volume_number: 'v2',
    start_year: '1992',
    issue_number: '044'
  };

  // `isPath` distinguishes the two previews that share this body. A rename
  // pattern is one filename and gets the whole cleanup. A move pattern is a
  // folder *path*, and mirrors models.comicvine.auto_move_file instead: the
  // pattern's own '/' are separators, so each *value* is sanitized with the
  // character map alone (helpers.sanitize_path_segment -- no spaces option; a
  // folder name never gets one) BEFORE it is substituted in, and empty
  // segments are dropped afterwards.
  //
  // Both halves of that order matter. Cleaning the assembled path as one
  // string would strip the separators and preview "MarvelSpider-Man 2099/";
  // cleaning it per segment after substitution would let a '/' inside a
  // value become a separator, previewing "Armageddon / X-Men" as two
  // folders -- the #588 bug the map exists to prevent.
  CLU.applyRenamePattern = function (values, pattern, isPath, cleanup) {
    cleanup = cleanup || {};
    if (isPath) {
      const map = cleanup.char_map || {};
      const cleaned = {};
      Object.keys(values).forEach(function (k) {
        cleaned[k] = (CLU.applyCharMap(String(values[k] ?? ''), map) || '').trim();
      });
      values = cleaned;
    }

    let result = pattern;

    result = result.replace(/\{publisher\}/g, values.publisher || '');
    result = result.replace(/\{series_name\}/g, values.series_name || '');
    result = result.replace(/\{volume_number\}/g, values.volume_number || '');
    result = result.replace(/\{volume_year\}/g, values.volume_year || values.year || '');  // For rename patterns
    result = result.replace(/\{year\}/g, values.year || '');  // Legacy rename token
    result = result.replace(/\{start_year\}/g, values.start_year || '');  // For move patterns
    result = result.replace(/\{issue_year\}/g, values.issue_year || '');
    result = result.replace(/\{issue_month_M\}/g, values.issue_month_M || '');
    result = result.replace(/\{issue_month_m\}/g, values.issue_month_m || '');
    result = result.replace(/\{issue_number\}/g, values.issue_number || '');
    result = result.replace(/\{issue_title\}/g, values.issue_title || '');

    // Drop any leftover {token} we don't recognize (mirrors apply_custom_pattern)
    result = result.replace(/\{[A-Za-z_][^}]*\}/g, '');

    // Clean up extra spaces
    result = result.replace(/\s+/g, ' ').trim();

    // Remove a separator left dangling against a parenthesis boundary
    // (e.g. "(2020-)" -> "(2020)", "(, 2026)" -> "(2026)") when a token resolved empty
    result = result.replace(/\(\s*[-,]\s*/g, '(');
    result = result.replace(/\s*[-,]\s*\)/g, ')');

    // Remove empty parentheses
    result = result.replace(/\s*\(\s*\)/g, '').trim();

    // Remove orphaned separators (e.g., trailing " - " when issue_title is empty)
    result = result.replace(/\s*-\s*(?=\(|$)/g, ' ').replace(/\s+/g, ' ').trim();

    if (isPath) {
      // Values are already sanitized above; only empty segments are left
      // to drop, as auto_move_file does.
      return result.split('/').filter(function (seg) { return seg.trim(); }).join('/');
    }

    // Filename character cleanup -- one shared mirror of
    // cbz_ops/rename.py:apply_filename_cleanup, so this preview cannot
    // drift from what the renamer actually writes.
    return CLU.applyFilenameCleanup(result, cleanup);
  };

  /** The text a rename preview shows for `pattern`. */
  CLU.renamePreviewText = function (pattern, cleanup) {
    pattern = (pattern || '').trim();
    if (!pattern) return 'Enter a pattern to see preview...';
    try {
      return CLU.applyRenamePattern(CLU.RENAME_SAMPLE_VALUES, pattern, false, cleanup) + '.cbz';
    } catch (error) {
      return 'Invalid pattern';
    }
  };

  /** The text a move preview shows for `pattern`. */
  CLU.movePreviewText = function (pattern, cleanup) {
    pattern = (pattern || '').trim();
    if (!pattern) return 'Enter a pattern to see preview...';
    try {
      return CLU.applyRenamePattern(CLU.MOVE_SAMPLE_VALUES, pattern, true, cleanup) + '/';
    } catch (error) {
      return 'Invalid pattern';
    }
  };
})(window);
