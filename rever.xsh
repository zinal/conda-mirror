$ACTIVITIES = [
    "authors",
    "changelog",
    "tag",
    "push_tag",
    "pypi",
    "ghrelease",
    #"conda_forge",
    ]

#
# Basic settings
#
$PROJECT = $GITHUB_REPO = "conda-mirror"
$GITHUB_ORG = "regro"
$AUTHORS_FILENAME = "AUTHORS.md"
$PYPI_SIGN = False

#
# Changelog settings
#
$CHANGELOG_FILENAME = "CHANGELOG.md"
$CHANGELOG_TEMPLATE = "TEMPLATE.md"
$CHANGELOG_PATTERN = "<!-- current developments -->"
$CHANGELOG_HEADER = """<!-- current developments -->

## [$VERSION](https://github.com/regro/conda-mirror/tree/$VERSION) ($RELEASE_DATE)

"""
$CHANGELOG_CATEGORIES = (
    "Implemented enhancements",
    "Fixed bugs",
    "Closed issues",
    "Merged pull requests",
    )


def title_formatter(category):
    return '**' + category + ':**\n\n'


$CHANGELOG_CATEGORY_TITLE_FORMAT = title_formatter
$CHANGELOG_AUTHORS_TITLE = "Contributors"
