# Third-party components

CellPilot's own code is Apache-2.0. It uses these components under their own licences.

## Runtime (Python, `requirements.txt`)

| Component | Licence |
|---|---|
| FastAPI | MIT |
| Starlette | BSD-3-Clause |
| Uvicorn | BSD-3-Clause |
| google-genai (Google Gen AI SDK) | Apache-2.0 |
| google-cloud-storage | Apache-2.0 |
| Pydantic | MIT |
| truststore | MIT |

## Development (`requirements-dev.txt`)

| Component | Licence |
|---|---|
| pytest | MIT |
| httpx | BSD-3-Clause |

## Services

Gemini 4 Argon on Gemini Enterprise Agent Platform and Cloud Run are Google Cloud services used under the Google Cloud terms. They are not redistributed here.

## Browser

The page is plain HTML, CSS and JavaScript with no third-party libraries.

## Media

The sample clips in `samples/kitting-20261017/` were recorded by the team on 17 October 2026 with NODAL M1 cameras and show a team member at a bench, with their consent; they contain no third-party content. Product photos of the NODAL M1 are not included in this repository.

## Planned, not yet included

If the 3D layout or the simulated cobot ships, it will add MuJoCo (Apache-2.0), MuJoCo Menagerie models for the UR5e (BSD-3-Clause), Robotiq 2F-85 (BSD-2-Clause) and Franka FR3 (Apache-2.0), mink (Apache-2.0), OpenCV (Apache-2.0) and aniposelib (BSD-2-Clause), each with its licence file kept beside it.
