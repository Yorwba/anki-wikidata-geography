{ pkgs ? import <nixpkgs> {}, ... }:

let
  pythonPackages = pkgs.python3Packages;
  wikidata =
    let
      pname = "wikidata";
      version = "0.9.0";
    in
    pythonPackages.buildPythonPackage {
      inherit pname version;
      src = pkgs.fetchPypi {
        inherit pname version;
        sha256 = "sha256-BZ5OlXSqKgPkMn0o34wMxmHbKCanWQDNoBQr3AG0u38=";
      };
      doCheck = false;
      pyproject = true;
      build-system = [ pythonPackages.setuptools ];
    };
in
pkgs.mkShell {
  buildInputs = with pythonPackages; [
    python
    wikidata
    genanki
    numpy
    pillow
    colorlog
  ] ++ [ pkgs.resvg ];
}
