import sys
import os
import re
from core.app_logging import app_logger
from core.config import config, load_config
from helpers import is_hidden

load_config()

raw_terms = config.get("SETTINGS", "IGNORED_TERMS", fallback="Annual")
terms = [t.strip() for t in raw_terms.split(",") if t.strip()]

def check_missing_issues(root_directory):
    """
    Recursively scans 'root_directory' (including sub-directories) for 
    filenames in one of these forms:
        <Series Name> <Issue Number> (<Year>).cbz
        <Series Name> <Issue Number> (<Year>).cbr
        <Series Name> <Issue Number>.cbz
        <Series Name> <Issue Number>.cbr

    When multiple files in the same directory have the same series name, 
    but some include a year and others do not, they are treated as the 
    same series, adopting the year if at least one file has it.

    Any filename containing any of the substrings in IGNORE (case-insensitive)
    is skipped. We also normalize curly quotes ('’‘“”) to straight quotes 
    ('"') before checking.

    All missing issues (e.g., #003, #004) are written to a single 
    'missing.txt' in 'root_directory'. If a consecutive run of missing issues
    is 50 or more, a single condensed line is used to report that run.

    Now updated to print the directory path once (for each series) only if
    there are missing issues for that series.
    """

    app_logger.info("********************// Missing File Check //********************")

    pattern = re.compile(r'^(.+?)\s+#?(\d+)(?:\s*\((\d+)\))?\.(?:cbz|cbr)$', re.IGNORECASE)
    data_dict = {}
    not_matched = []

    for dirpath, dirnames, filenames in os.walk(root_directory):
        # Skip hidden directories.
        dirnames[:] = [d for d in dirnames if not is_hidden(os.path.join(dirpath, d))]
        for fname in filenames:
            full_path = os.path.join(dirpath, fname)
            # Skip hidden files.
            if is_hidden(full_path):
                continue

            fname_stripped = fname.strip()
            normalized_fname = (
                fname_stripped.replace("‘", "'")
                            .replace("’", "'")
                            .replace("“", '"')
                            .replace("”", '"')
            )

            if any(ignore_word and ignore_word.lower() in normalized_fname.lower() for ignore_word in terms):
                continue

            match = pattern.match(fname_stripped)
            if match:
                series_name = match.group(1).strip()
                issue_str = match.group(2)
                year_str = match.group(3)

                try:
                    issue_num = int(issue_str)
                except ValueError:
                    not_matched.append(full_path)
                    continue

                key = (dirpath, series_name.lower())
                if key not in data_dict:
                    data_dict[key] = {"series_name": series_name, "years": set(), "issues": set()}
                
                if year_str:
                    data_dict[key]["years"].add(year_str)
                data_dict[key]["issues"].add(issue_num)
            else:
                not_matched.append(full_path)

    missing_file_path = os.path.join(root_directory, "missing.txt")
    num_missing_total = 0

    with open(missing_file_path, 'w') as f_out:
        for (dirpath, series_name_lower) in sorted(data_dict.keys()):
            info = data_dict[(dirpath, series_name_lower)]
            series_name_original = info["series_name"]
            years_found = info["years"]
            issues_found = sorted(info["issues"])

            adopted_year = min(years_found) if years_found else None
            if not issues_found:
                continue

            max_issue = issues_found[-1]
            missing_runs = []
            start_run = None

            for i in range(1, max_issue + 1):
                if i not in issues_found:
                    if start_run is None:
                        start_run = i
                else:
                    if start_run is not None:
                        missing_runs.append((start_run, i - 1))
                        start_run = None

            if start_run is not None:
                missing_runs.append((start_run, max_issue))

            if missing_runs:
                f_out.write(f"Directory: {dirpath}\n")
            
            for (start_miss, end_miss) in missing_runs:
                run_length = end_miss - start_miss + 1
                num_missing_total += run_length

                if run_length >= 50:
                    missing_str = (
                        f"{series_name_original} {start_miss:03d}-{end_miss:03d} ({adopted_year}) [Total missing: {run_length}]"
                        if adopted_year
                        else f"{series_name_original} {start_miss:03d}-{end_miss:03d} [Total missing: {run_length}]"
                    )
                    f_out.write(missing_str + "\n")
                else:
                    for m in range(start_miss, end_miss + 1):
                        missing_str = (
                            f"{series_name_original} {m:03d} ({adopted_year}).cbz"
                            if adopted_year
                            else f"{series_name_original} {m:03d}.cbz"
                        )
                        f_out.write(missing_str + "\n")

            if missing_runs:
                f_out.write("\n")

    if num_missing_total == 0:
        with open(missing_file_path, 'w') as f_out:
            f_out.write("No missing issues found.\n")
        app_logger.info("No missing issues found.")
    else:
        app_logger.info(f"Found <code>{num_missing_total}</code> missing issues in <code>{root_directory}</code>.")

    
if __name__ == "__main__":
    if len(sys.argv) < 2:
        app_logger.error("No directory provided!")
    else:
        directory = sys.argv[1]
        check_missing_issues(directory)
        app_logger.info("********************// Missing Issue Check //********************")
