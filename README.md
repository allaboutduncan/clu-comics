# Comic Library Utilities (CLU)

![Docker Pulls](https://img.shields.io/docker/pulls/allaboutduncan/comic-utils-web)
![GitHub Release](https://img.shields.io/github/v/release/allaboutduncan/clu-comics)
![GitHub commits since latest release](https://img.shields.io/github/commits-since/allaboutduncan/clu-comics/latest)

[![Join our Discord](https://img.shields.io/discord/678794935368941569?label=CLU%20Discord&logo=discord&style=for-the-badge)](https://discord.gg/ndDhpvrgBa)


![Comic Library Utilities (CLU)](images/clu-logo-360.png "Comic Library Utilities")

## What is CLU & Why Does it Exist

This is a set of utilities I developed while moving my 70,000+ comic library to [Komga](https://komga.org/).

As I've continued to work on it, add features and discuss with other users, I wanted to pivot away from usage as an accessory to Komga and focus on it as a stand-alone app.

The app is intended to allow users to manage their remote comic collections, performing many actions in bulk, without having direct access to the server. You can convert, rename, move, enhance CBZ files within the app. Additionally, you can use the app to download comics from GetComics.org, update metadata using Metron and ComicVine, and more.

![Comic Library Utilities (CLU)](/images/header.png "Comic Library Utilities Publisher Page")

### Full Documentation
Full documention and install steps have [moved to clucomics.org](https://clucomics.org)

## Features
Here's a quick list of features

1. Directory Operations - Clean Files, Rename Files, Convert Files, Rebuild Files, Missing Issue Check, Enhance Images.
2. Single File Operations - Rebuild/Convert (CBR --> CBZ), Crop Cover, Remove First Image, Full GUI Editing of CBZ (rename/rearrange files, add/delete files, crop images), Add blank Image at End, Enhance Images, Delete File.
3. Pull List - Subsrcibe to weekly releases of new comics, auto-download them as single issues or weekly packs. Search for missing isssues of existing series.
4. Remote Downloads - Download comics from GetComics.org, update metadata using Metron and ComicVine, and more.
5. File Management - Source and Destination file browsing, Drag and drop to move directories and files, Rename directories and files, Delete directories or files, Rename All Filenames in Directory, Remove Text from All Filenames in Directory.
6. Folder Monitoring - Auto-Renaming, Auto-Convert to CBZ, Processing Sub-Directories, Auto-Upack, Move Sub-Directories, Custom Naming Patterns.
7. Insights - see you collection size, reading history by year, favorite authors, artists, charactes, and view a full timeline of everything you've read.
8. Optional local GCD Database Support

## Installation via Docker Compose

Copy the following and edit the environment variables

```yaml
version: '3.9'
services:
    comic-utils:
        image: allaboutduncan/comic-utils-web:latest

        container_name: clu
        logging:
            driver: "json-file"
            options:
                max-size: '20m'  # Reduce log size to 20MB
                max-file: '3'     # Keep only 3 rotated files
        restart: always
        ports:
            - '5577:5577'
        volumes:
            - "/path/to/local/config:/config" # Maps local folder to persist settings
            - "/path/to/local/cache:/cache" # Maps to local folder for DB and thumbnail cache
            ## update the line below to map to your library.
            ## Map your first/main library to /data
            - "/e/Comics:/data"
            ## Map additional libraries and add them in the settings of the app
            - "/e/Manga:/manga"
            - "/f/Magazines:/magazines"
            ## Additional folder if you want to use Folder Monitoring.
            - "/f/Downloads:/downloads"
        environment:
            - FLASK_ENV=production
            ## Set to 'yes' if you want to use folder monitoring.
            - MONITOR=yes/no
            ## Set the User ID (PUID) and Group ID (PGID) for the container.
            ## This is often needed to resolve permission issues, especially on systems like Unraid
            ## where a specific user/group owns the files.
            ## For Unraid, PUID is typically 99 (user 'nobody') and PGID is typically 100 (group 'users').
            ## For Windows/WSL, you need to set these to match your Windows user ID (see WINDOWS_WSL_SETUP.md)
            - PUID=99
            - PGID=100
            ## Set the file creation mask (UMASK). 022 is a common value.
            - UMASK=022
```

__Update your Docker Compose:__ Mapping the `/config` directory is required now to ensure that config settings are persisted on updates.
__First Install:__ On the first install with new config settings, visit the config page, ensure everything is configured as desired.
* Save your Config settings
* Click the Restart App button

### More About Volumes Mapping for Your Library
For the utility to work, you need to map your default library to `/data`, any additional libraries can be mapped and configured in the app.

### Examples of a Full Setup

![Insights](/images/insights.png "Insights showing collection information")

![Timeline](/images/timeline.png "Timeline showing reading history")

![Pull List](/images/weekly.png "Weekly Pull List showing weekly releases")

## Say Thanks
If you enjoyed this, want to say thanks or want to encourage updates and enhancements, feel free to [!["Buy Me A Coffee"](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/allaboutduncan)

### Full Documentation
Full documention and install steps are available at CLUcomics.org](https://clucomics.org)
