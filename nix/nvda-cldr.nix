{ fetchFromGitHub }:

# The revision upstream's include/nvda-cldr submodule pins, on the main-out
# branch that carries the generated locale/<lang>/cldr.dic files.
fetchFromGitHub {
  owner = "nvaccess";
  repo = "nvda-cldr";
  rev = "98f615984e19cfe94909ee414986e146982c2a79";
  hash = "sha256-KT0Bg7LnOOUkBaltL9lgTWYlwLe7uILjetYh4axLORo=";
}
