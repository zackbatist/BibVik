# Load packages
library(tidyverse)
library(magrittr)
library(rcrossref)
library(citecorp)
library(plyr)
library(httr)
library(jsonlite)

# Get list of refs from the OpenCitations API
response <- GET("https://opencitations.net/index/api/v2/references/doi:10.1007/s10814-021-09163-3")
content <- content(response, "text")
parsed_data <- fromJSON(content)
seed_refs <- sub('.*doi:', '', parsed_data$cited)
seed_refs <- gsub( " .*$", "", seed_refs)
seed_refs <- data.frame(seed_refs)
colnames(seed_refs)[1] <- "doi"

# Retrieve bibliographic metadata for each referenced work from CrossRef
seed_refs_cr <- cr_works(seed_refs$doi)
seed_refs_cr <- seed_refs_cr$data
seed_refs$title <- seed_refs_cr$title[match(seed_refs$doi, seed_refs_cr$doi)]
seed_refs$container_title <- seed_refs_cr$container.title[match(seed_refs$doi, seed_refs_cr$doi)]
seed_refs$type <- seed_refs_cr$type[match(seed_refs$doi, seed_refs_cr$doi)]
seed_refs$date <- seed_refs_cr$created[match(seed_refs$doi, seed_refs_cr$doi)]
seed_refs$year <- substr(seed_refs$date, 1, 4)

# Not necessary to retrieve abstracts via API.
# They are too messy with html tags, and RA will review the PDFs.

# Generate lists of authors, indexed by DOIs
