class Ttyplayer < Formula
  include Language::Python::Virtualenv

  desc "Modern YouTube player for the terminal"
  homepage "https://github.com/webliftro/ttyplayer"
  url "https://files.example/ttyplayer-1.0.0.tar.gz"
  sha256 "ttyplayer-1.0.0-sha"
  license "MIT"

  depends_on "mpv"
  depends_on "pillow"
  depends_on "python@3.13"
  # Only `ttyplayer serve --stream` uses ffmpeg, and doctor reports it as optional.
  depends_on "ffmpeg" => :optional

  resource "aiohttp" do
    url "https://files.example/aiohttp-3.14.4.tar.gz"
    sha256 "aiohttp-3.14.4-sha"
  end

  resource "idna" do
    url "https://files.example/idna-3.20.tar.gz"
    sha256 "idna-3.20-sha"
  end

  resource "linkify-it-py" do
    url "https://files.example/linkify-it-py-2.2.0.tar.gz"
    sha256 "linkify-it-py-2.2.0-sha"
  end

  resource "markdown-it-py" do
    url "https://files.example/markdown-it-py-4.2.0.tar.gz"
    sha256 "markdown-it-py-4.2.0-sha"
  end

  resource "mdurl" do
    url "https://files.example/mdurl-0.1.2.tar.gz"
    sha256 "mdurl-0.1.2-sha"
  end

  resource "rich" do
    url "https://files.example/rich-14.0.0.tar.gz"
    sha256 "rich-14.0.0-sha"
  end

  resource "textual" do
    url "https://files.example/textual-8.2.0.tar.gz"
    sha256 "textual-8.2.0-sha"
  end

  resource "textual-image" do
    url "https://files.example/textual-image-0.14.1.tar.gz"
    sha256 "textual-image-0.14.1-sha"
  end

  resource "yarl" do
    url "https://files.example/yarl-1.22.0.tar.gz"
    sha256 "yarl-1.22.0-sha"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/ttyplayer version")
  end
end
