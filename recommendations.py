import os
import re
import json
import logging
from core.config import config

# Try imports, logging warning if missing since these are optional dependencies until now
try:
    import openai
except ImportError:
    openai = None

try:
    import anthropic
except ImportError:
    anthropic = None

# Set up logging
logger = logging.getLogger(__name__)


def extract_series_from_path(path):
    """
    Extract series name with year from path.

    Examples:
        /data/Marvel/Ultimate Spider-Man/v2024/Ultimate Spider-Man 022 (2024).cbz
        -> "Ultimate Spider-Man (2024)"

        /data/Boom!/Something Is Killing the Children (2019)/issue.cbz
        -> "Something Is Killing the Children (2019)"
    """
    # Get parent folder name
    parent = os.path.basename(os.path.dirname(path))

    # Check if parent looks like a year volume (v2024, v2019, etc.)
    if re.match(r'^v\d{4}$', parent):
        # Go up one more level for the series name
        grandparent = os.path.basename(os.path.dirname(os.path.dirname(path)))
        year = parent[1:]  # Remove 'v' prefix
        return f"{grandparent} ({year})"

    # Check if parent already has year in it like "Series Name (2019)"
    year_match = re.search(r'\((\d{4})\)$', parent)
    if year_match:
        return parent  # Already formatted correctly

    # Try to extract year from filename
    filename = os.path.basename(path)
    year_match = re.search(r'\((\d{4})\)', filename)
    if year_match:
        return f"{parent} ({year_match.group(1)})"

    # Fallback: just return parent folder name
    return parent


def get_recommendations(api_key, provider, model, reading_history):
    """
    Get comic recommendations based on reading history using the specified LLM provider.
    
    Args:
        api_key (str): API Key for the provider
        provider (str): 'gemini', 'openai', or 'anthropic'
        model (str): Specific model name to use
        reading_history (list): List of titles/series recently read
        
    Returns:
        list: List of recommendation dictionaries
    """
    
    if not api_key:
        logger.error("No API key provided for recommendations")
        return {"error": "API Key is required"}
        
    if not reading_history:
        return []
        
    # Check for library availability
    if (provider == 'gemini' or provider == 'openai') and openai is None:
        return {"error": "The 'openai' python package is required. Please run: pip install openai"}
    if provider == 'anthropic' and anthropic is None:
        return {"error": "The 'anthropic' python package is required. Please run: pip install anthropic"}

    # Extract unique series from reading history
    series_set = set()
    for item in reading_history:
        series_name = extract_series_from_path(item['path'])
        series_set.add(series_name)

    # Sort alphabetically for consistent output
    history_text = "\n".join([f"- {s}" for s in sorted(series_set)])

    # Get TO READ items and extract unique series
    from core.database import get_to_read_items
    to_read_items = get_to_read_items()
    to_read_set = set()
    for item in to_read_items:
        series_name = extract_series_from_path(item['path'])
        to_read_set.add(series_name)

    to_read_text = "\n".join([f"- {s}" for s in sorted(to_read_set)])
    
    system_prompt = (
        "You are an expert comic book librarian with deep knowledge of comics, graphic novels, and manga. "
        "Based on the user's recent reading history provided below, suggest 5 new series they might enjoy. "
        "Focus on finding hidden gems, critically acclaimed runs, or similar tones/themes, avoid suggesting the exact same series they just read. "
        "Avoid suggesting the same publisher for all recommendations. "
        "Avoid suggesting the same genre for all recommendations. "
        "Avoid suggesting series from the list of titles the user has marked as TO READ. "
        "\n\n"
        "Return the response ONLY as a valid JSON array of objects with these keys:\n"
        "- title: The title of the series\n"
        "- publisher: The publisher (e.g., Image, Marvel, DC, Fantagraphics)\n"
        "- reason: A brief, persuasive reason why they'll like it based on their history (1-2 sentences)\n"
        "- volume: (Optional) A specific starting volume or 'Vol 1' if applicable\n"
    )
    
    # Build user prompt with reading history and TO READ list
    user_prompt = f"Here is my reading history (series I have read):\n{history_text}"
    if to_read_text:
        user_prompt += f"\n\nTO READ (do not recommend these, I already plan to read them):\n{to_read_text}"
    user_prompt += "\n\nSuggest 5 recommendations in JSON format."

    try:
        if provider == "gemini":
            return _call_gemini_via_openai(api_key, model, system_prompt, user_prompt)
        elif provider == "openai":
            return _call_openai(api_key, model, system_prompt, user_prompt)
        elif provider == "anthropic":
            return _call_anthropic(api_key, model, system_prompt, user_prompt)
        else:
            return {"error": f"Unsupported provider: {provider}"}
            
    except Exception as e:
        logger.error(f"Error getting recommendations from {provider}: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return {"error": f"API Error: {str(e)}"}

def _parse_json_response(content):
    """Helper to safely parse JSON from LLM response"""
    try:
        # Clean up markdown code blocks if present
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[0].strip() # Fallback
            
        return json.loads(content)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse JSON response: {content}")
        return {"error": "Failed to parse recommendations from AI response"}

def _call_gemini_via_openai(api_key, model, system_prompt, user_prompt):
    # Gemini supports OpenAI-compatible library calls
    client = openai.OpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    
    # Gemini via OpenAI compat works best with a single message or mapped roles
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        response_format={"type": "json_object"} # Try to enforce JSON mode
    )
    
    content = response.choices[0].message.content
    return _parse_json_response(content)

def _call_openai(api_key, model, system_prompt, user_prompt):
    client = openai.OpenAI(api_key=api_key)
    
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        response_format={"type": "json_object"}
    )
    
    content = response.choices[0].message.content
    
    # OpenAI json_object mode requires the output to be a valid JSON object, 
    # but our prompt asks for an array. Sometimes wrapper objects are returned.
    parsed = _parse_json_response(content)
    
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        # Check for common wrapper keys like 'recommendations' or 'series'
        for key in ['recommendations', 'series', 'suggestions']:
            if key in parsed and isinstance(parsed[key], list):
                return parsed[key]
        # customized fallback
        return [parsed] 
        
    return parsed

def _call_anthropic(api_key, model, system_prompt, user_prompt):
    client = anthropic.Anthropic(api_key=api_key)
    
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[
            {"role": "user", "content": user_prompt}
        ]
    )
    
    content = message.content[0].text
    return _parse_json_response(content)