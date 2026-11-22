{
  lib,
  stdenv,
  fetchFromGitLab,
  autoreconfHook,
  autoconf-archive,
  pkg-config,
  jdk,
  xprop,
  at-spi2-core,
  dbus,
  libx11,
  glib,
}:

stdenv.mkDerivation (finalAttrs: {
  pname = "java-atk-wrapper";
  version = "0.44.0";

  src = fetchFromGitLab {
    domain = "gitlab.gnome.org";
    owner = "GNOME";
    repo = "java-atk-wrapper";
    rev = finalAttrs.version;
    hash = "sha256-uiv4ODrVfZRqY2vKEPXspjd0mYQRMVhWaI5CbnA49PQ=";
  };

  nativeBuildInputs = [
    autoreconfHook
    autoconf-archive
    pkg-config
    jdk
    xprop
  ];
  buildInputs = [
    at-spi2-core
    dbus
    libx11
    glib
  ];

  configureFlags = [
    "--with-jdk-auto-detect=no"
    "JDK_SRC=${jdk.home}"
    "--with-jardir=${placeholder "out"}/share/java"
    "--with-propertiesdir=${placeholder "out"}/share/java"
  ];
})
